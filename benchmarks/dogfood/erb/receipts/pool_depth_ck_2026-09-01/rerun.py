import sys, os
ERB="F:/Projects/cymatix-context/.claude/worktrees/wave-1-semantic-ranking-5f1dab/benchmarks/dogfood/erb"
SP=os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0,ERB)
import probe_phrase_reach as P
P.OUT_PATH=os.path.join(SP,"rerun_phrase_reach.json")
P.main()
import probe_phrase_reach_addendum as A
A.RECEIPT=P.OUT_PATH
A.main()
