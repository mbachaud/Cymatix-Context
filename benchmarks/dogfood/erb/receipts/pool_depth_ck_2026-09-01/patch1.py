import io, ast
p = 'F:/Projects/cymatix-context/.claude/worktrees/wave-1-semantic-ranking-5f1dab/benchmarks/dogfood/erb/probe_head_chunk_lane.py'
s = io.open(p, encoding='utf-8').read()

old = """        grams_idf = []
        for t in set(terms):
            g = tuple(TOK.findall(t.lower()))
            if g:
                grams_idf.append((g, idf_of(t)))"""
new = """        # NOTE: iterate the RAW list, not a set. query_docs builds
        # ' OR '.join('"t"' for t in query_terms) over domains+entities WITH
        # duplicates, and FTS5 bm25() sums a repeated phrase twice. De-duping
        # here breaks bit-exactness against the engine (measured: max |d| 25.9).
        grams_idf = []
        for t in terms:
            g = tuple(TOK.findall(t.lower()))
            if g:
                grams_idf.append((g, idf_of(t)))"""
assert old in s
s = s.replace(old, new)

old = """    def bm25_from_tokens(toks, grams_idf, D):
        s = 0.0
        L = len(toks)
        for gram, idf in grams_idf:
            g = len(gram)
            if g == 0 or g > L:
                continue
            if g == 1:
                tf = toks.count(gram[0])
            else:
                tf = 0
                g0 = gram[0]
                for i in range(L - g + 1):
                    if toks[i] == g0 and tuple(toks[i:i + g]) == gram:
                        tf += 1
            if tf:
                s += idf * (tf * (K1_BM25 + 1.0)) / (
                    tf + K1_BM25 * (1.0 - B_BM25 + B_BM25 * D / avgdl))
        return -s"""
new = """    def bm25_from_tokens(toks, grams_idf, D):
        s = 0.0
        L = len(toks)
        cnt = Counter(toks)
        norm = K1_BM25 * (1.0 - B_BM25 + B_BM25 * D / avgdl)
        for gram, idf in grams_idf:
            g = len(gram)
            if g == 0 or g > L:
                continue
            if g == 1:
                tf = cnt[gram[0]]
            else:
                g0 = gram[0]
                if not cnt[g0]:
                    continue
                tf = 0
                i = 0
                gl = list(gram)
                while True:
                    try:
                        i = toks.index(g0, i)
                    except ValueError:
                        break
                    if toks[i:i + g] == gl:
                        tf += 1
                    i += 1
            if tf:
                s += idf * (tf * (K1_BM25 + 1.0)) / (tf + norm)
        return -s"""
assert old in s
s = s.replace(old, new)

old = """        texts, raw_content = {}, {}
        nl = sorted(need)
        for j in range(0, len(nl), 300):
            ch = nl[j:j + 300]
            ph = ",".join("?" * len(ch))
            for rid, g_, c_, cp_ in cur.execute(
                    "SELECT rowid, gene_id, content, complement FROM genes_fts_source "
                    "WHERE rowid IN (%s)" % ph, ch):
                texts[rid] = TOK.findall(
                    ((g_ or "") + " " + (c_ or "") + " " + (cp_ or "")).lower())
            for rid, c_ in cur.execute(
                    "SELECT rowid, content FROM genes WHERE rowid IN (%s)" % ph, ch):
                raw_content[rid] = c_ or \"\""""
new = """        parts, raw_content = {}, {}
        nl = sorted(need)
        for j in range(0, len(nl), 300):
            ch = nl[j:j + 300]
            ph = ",".join("?" * len(ch))
            for rid, c_ in cur.execute(
                    "SELECT rowid, content FROM genes WHERE rowid IN (%s)" % ph, ch):
                raw_content[rid] = c_ or ""
            for rid, g_, c_, cp_ in cur.execute(
                    "SELECT rowid, gene_id, content, complement FROM genes_fts_source "
                    "WHERE rowid IN (%s)" % ph, ch):
                parts[rid] = (TOK.findall((g_ or "").lower()),
                              TOK.findall((c_ or "").lower()),
                              TOK.findall((cp_ or "").lower()))"""
assert old in s
s = s.replace(old, new)

old = """        for _s, v in cands:
            f_rid, f_gid, f_sc, f_rnk, f_i = v["first"][0]
            mm = min(v["middle"], key=lambda x: x[2])   # strongest middle sibling
            m_rid, m_gid, m_sc, m_rnk, m_i = mm
            if f_rid not in texts or m_rid not in texts:
                continue
            for rid, engine in ((f_rid, f_sc), (m_rid, m_sc)):
                rep = bm25_from_tokens(texts[rid], grams_idf, dl_by_rowid[rid])
                replica_check["n_rows"] += 1
                replica_check["max_abs_delta"] = max(
                    replica_check["max_abs_delta"], abs(rep - engine))
            rc = raw_content.get(f_rid, "")
            content_toks = TOK.findall(rc.lower())
            n_content = len(content_toks)
            toks = texts[f_rid]
            n_hdr = len(TOK.findall(rc[:HEADER_CHARS].lower()))
            start = None
            approx = len(toks) - n_content
            for cand in range(max(0, approx - 6), approx + 7):
                if cand >= 0 and toks[cand:cand + n_content] == content_toks:
                    start = cand
                    break
            if start is None:
                pair_fail["no_content_anchor"] += 1
                continue
            abl = toks[:start] + toks[start + n_hdr:]
            f_abl = bm25_from_tokens(abl, grams_idf, dl_by_rowid[f_rid] - n_hdr)
            pairs_all.append({
                "needle": m["needle"],
                "head_gid": f_gid, "mid_gid": m_gid,
                "head_bm25": -f_sc, "mid_bm25": -m_sc,
                "head_bm25_no_header": -f_abl,
                "head_deep_rank": f_rnk, "mid_deep_rank": m_rnk,
                "n_header_tokens": n_hdr,
                "head_dl": dl_by_rowid[f_rid], "mid_dl": dl_by_rowid[m_rid],
            })"""
new = """        for _s, v in cands:
            f_rid, f_gid, f_sc, f_rnk, f_i = v["first"][0]
            mm = min(v["middle"], key=lambda x: x[2])   # strongest middle sibling
            m_rid, m_gid, m_sc, m_rnk, m_i = mm
            if f_rid not in parts or m_rid not in parts:
                continue
            f_g, f_c, f_p = parts[f_rid]
            m_g, m_c, m_p = parts[m_rid]
            f_toks = f_g + f_c + f_p
            m_toks = m_g + m_c + m_p
            if replica_check["n_rows"] < 3000:
                for rid_, toks_, engine in ((f_rid, f_toks, f_sc), (m_rid, m_toks, m_sc)):
                    rep = bm25_from_tokens(toks_, grams_idf, dl_by_rowid[rid_])
                    replica_check["n_rows"] += 1
                    replica_check["max_abs_delta"] = max(
                        replica_check["max_abs_delta"], abs(rep - engine))
            # the raw genes.content tokens are the TAIL of the view's content
            # column (source_id + tags are prepended); the header window is the
            # first HEADER_CHARS characters of genes.content.
            rc = raw_content.get(f_rid, "")
            n_content = len(TOK.findall(rc.lower()))
            n_prefix = len(f_c) - n_content
            if n_prefix < 0:
                pair_fail["no_content_anchor"] += 1
                continue
            n_hdr = len(TOK.findall(rc[:HEADER_CHARS].lower()))
            cstart = len(f_g) + n_prefix
            abl = f_toks[:cstart] + f_toks[cstart + n_hdr:]
            f_abl = bm25_from_tokens(abl, grams_idf, dl_by_rowid[f_rid] - n_hdr)
            mrc = raw_content.get(m_rid, "")
            m_ncontent = len(TOK.findall(mrc.lower()))
            m_nprefix = len(m_c) - m_ncontent
            f_np = f_g + f_c[n_prefix:] + f_p
            m_np = (m_g + m_c[m_nprefix:] + m_p) if m_nprefix >= 0 else m_toks
            f_noprefix = bm25_from_tokens(f_np, grams_idf, dl_by_rowid[f_rid] - n_prefix)
            m_noprefix = bm25_from_tokens(
                m_np, grams_idf, dl_by_rowid[m_rid] - max(0, m_nprefix))
            hc = Counter(f_toks[cstart:cstart + n_hdr])
            fc = Counter(f_toks)
            tf_hdr = sum(hc[g[0]] for g, _i in grams_idf if len(g) == 1)
            tf_all = sum(fc[g[0]] for g, _i in grams_idf if len(g) == 1)
            pairs_all.append({
                "needle": m["needle"],
                "head_gid": f_gid, "mid_gid": m_gid,
                "head_bm25": -f_sc, "mid_bm25": -m_sc,
                "head_bm25_no_header": -f_abl,
                "head_bm25_no_prefix": -f_noprefix,
                "mid_bm25_no_prefix": -m_noprefix,
                "head_deep_rank": f_rnk, "mid_deep_rank": m_rnk,
                "n_header_tokens": n_hdr, "n_prefix_tokens": n_prefix,
                "head_qterm_tf_total": tf_all, "head_qterm_tf_in_header": tf_hdr,
                "head_dl": dl_by_rowid[f_rid], "mid_dl": dl_by_rowid[m_rid],
            })"""
assert old in s
s = s.replace(old, new)

old = """        "mean_mid_bm25_in_those_pairs": mean(p["mid_bm25"] for p in won),
    }"""
new = """        "mean_mid_bm25_in_those_pairs": mean(p["mid_bm25"] for p in won),
        "mean_prefix_tokens_source_id_plus_tags": mean(p["n_prefix_tokens"] for p in pairs_all),
        "head_qterm_tf_share_inside_header_pct": (
            round(100.0 * sum(p["head_qterm_tf_in_header"] for p in pairs_all)
                  / max(1, sum(p["head_qterm_tf_total"] for p in pairs_all)), 2)),
        "header_token_share_of_head_doc_pct": (
            round(100.0 * sum(p["n_header_tokens"] for p in pairs_all)
                  / max(1, sum(p["head_dl"] for p in pairs_all)), 2)),
        "prefix_counterfactual_both_stripped": {
            "note": ("delete the view's synthetic source_id+tags prefix from BOTH chunks "
                     "(the doc-level part of the FTS text) and re-run the sign test"),
            "head_wins": sum(1 for p in pairs_all
                             if p["head_bm25_no_prefix"] > p["mid_bm25_no_prefix"]),
            "n_decided": sum(1 for p in pairs_all
                             if p["head_bm25_no_prefix"] != p["mid_bm25_no_prefix"]),
        },
    }"""
assert old in s
s = s.replace(old, new)

io.open(p, 'w', encoding='utf-8').write(s)
ast.parse(s)
print('patched ok')
