---
type: ""
title: Empty type field
tags: [degraded, still-tagged]
---

The frontmatter parses but `type` is empty — non-conformant under
spec §9 rule 2. Helix ingests it as a generic document; the other
frontmatter fields (title, tags) are still honored.
