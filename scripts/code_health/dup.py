"""Near-duplicate functions: what token-identical clone detection misses.

Two normalizations, two LSH indexes:
  lexical     tokens as written, lowercased -- the headline similarity number
  structural  identifiers -> ID, literals -> LIT -- catches renamed copies

One index tuned for J=0.60 recovers only ~56% of candidates at J=0.40, so the
structural branch gets its own index rather than being filtered through the
lexical one.
"""

import hashlib
import random
import re

NUM_PERM = 128
BANDS = 32
ROWS = 4
SHINGLE_K = 5
SIZE_RATIO = 2.0

# Second branch: high structural similarity with a low lexical floor.
# Measured on a 7-line cache wrapper with three identifiers renamed:
# structural 1.00, lexical 0.32. A 0.40 floor rejects exactly the case this
# branch exists to catch, so the floor is 0.25 and the structural bar is 0.90.
# Unrelated functions measure structural 0.07 and clear neither branch.
STRUCTURAL_MIN = 0.90
STRUCTURAL_LEX_FLOOR = 0.25

TOKEN_RE = re.compile(
    r'"""[\s\S]*?"""'          # triple-quoted string
    r"|'''[\s\S]*?'''"
    r'|"(?:\\.|[^"\\])*"'      # single-line string
    r"|'(?:\\.|[^'\\])*'"
    r'|[^\W\d]\w*'             # identifier -- Unicode-aware, keeps Cyrillic
    r'|\d+(?:\.\d+)?'          # number
    r'|[^\s\w]'                # punctuation
)

_KEYWORDS = {
    "python": {
        "def", "class", "return", "if", "elif", "else", "for", "while", "try",
        "except", "finally", "with", "as", "import", "from", "raise", "yield",
        "lambda", "pass", "break", "continue", "in", "is", "not", "and", "or",
        "none", "true", "false", "self", "async", "await", "global", "nonlocal",
        "assert", "del",
    },
    "javascript": {
        "function", "return", "if", "else", "for", "while", "switch", "case",
        "break", "continue", "const", "let", "var", "class", "new", "this",
        "try", "catch", "finally", "throw", "typeof", "instanceof", "async",
        "await", "import", "export", "default", "extends", "null", "undefined",
        "true", "false", "of", "in",
    },
}
_KEYWORDS["typescript"] = _KEYWORDS["javascript"] | {
    "interface", "type", "enum", "implements", "public", "private", "protected",
    "readonly", "namespace", "declare",
}

_EXT_LANG = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
}

_rnd = random.Random(0xC0DEC0DE)
_SALTS = [_rnd.getrandbits(64) for _ in range(NUM_PERM)]


def language_of(path):
    for ext, lang in _EXT_LANG.items():
        if path.endswith(ext):
            return lang
    return "unknown"


def tokenize(src):
    return [t.lower() if not t[:1] in "\"'" else t for t in TOKEN_RE.findall(src)]


def structural(tokens, lang):
    keywords = _KEYWORDS.get(lang, set())
    out = []
    for token in tokens:
        first = token[:1]
        if first in "\"'" or (first.isdigit()):
            out.append("LIT")
        elif token[:1].isalpha() or first == "_":
            out.append(token if token in keywords else "ID")
        else:
            out.append(token)
    return out


def shingle_hashes(tokens, k=SHINGLE_K):
    if len(tokens) < k:
        joined = "\x00".join(tokens)
        return {_hash(joined)} if tokens else set()
    return {
        _hash("\x00".join(tokens[i:i + k]))
        for i in range(len(tokens) - k + 1)
    }


def _hash(text):
    return int.from_bytes(
        hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "big"
    )


def signature(hashes):
    """XOR-salt MinHash. 0.90 ms per function at 100 shingles, vs 3.33 ms for
    the (a*h + b) mod prime family -- and only candidate generation depends on
    it, because every reported pair gets an exact Jaccard."""
    if not hashes:
        return tuple([0] * NUM_PERM)
    return tuple(min(h ^ salt for h in hashes) for salt in _SALTS)


def jaccard(a, b):
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def lsh_pairs(sigs, bands=BANDS, rows=ROWS):
    buckets = {}
    for key, sig in sigs.items():
        for band in range(bands):
            chunk = sig[band * rows:(band + 1) * rows]
            buckets.setdefault((band, chunk), []).append(key)
    pairs = set()
    for members in buckets.values():
        if len(members) < 2:
            continue
        ordered = sorted(members)
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                pairs.add((ordered[i], ordered[j]))
    return pairs


def cluster_pairs(pairs):
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    groups = {}
    for key in parent:
        groups.setdefault(find(key), []).append(key)
    return [sorted(members) for members in groups.values() if len(members) > 1]


def _read_slice(repo_root, row):
    try:
        text = (repo_root / row["filename"]).read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    return "\n".join(lines[row["start_line"] - 1:row["end_line"]])


def find_near_duplicates(repo_root, functions, cfg):
    options = cfg["near_duplicate"]
    min_tokens = options["min_tokens"]
    min_similarity = options["min_similarity"]

    entries = {}
    for row in functions:
        if row.get("token", 0) < min_tokens:
            continue
        src = _read_slice(repo_root, row)
        if not src:
            continue
        key = "%s:%s:%d" % (row["filename"], row["function_name"], row["start_line"])
        tokens = tokenize(src)
        lang = language_of(row["filename"])
        lex = shingle_hashes(tokens)
        st = shingle_hashes(structural(tokens, lang))
        entries[key] = {
            "row": row, "tokens": row.get("token", 0),
            "lex": lex, "st": st,
            "lex_sig": signature(lex), "st_sig": signature(st),
        }

    if len(entries) < 2:
        return {"clusters": [], "compared": len(entries)}

    candidates = lsh_pairs({k: v["lex_sig"] for k, v in entries.items()})
    candidates |= lsh_pairs({k: v["st_sig"] for k, v in entries.items()})

    scored = []
    for a, b in candidates:
        ea, eb = entries[a], entries[b]
        big, small = max(ea["tokens"], eb["tokens"]), min(ea["tokens"], eb["tokens"])
        if small == 0 or big / float(small) > SIZE_RATIO:
            continue
        lex = jaccard(ea["lex"], eb["lex"])
        st = jaccard(ea["st"], eb["st"])
        if lex >= min_similarity or (st >= STRUCTURAL_MIN and lex >= STRUCTURAL_LEX_FLOOR):
            scored.append((a, b, lex, st))

    clusters = []
    for members in cluster_pairs([(a, b) for a, b, _, _ in scored]):
        inside = [s for s in scored if s[0] in members and s[1] in members]
        avg_tokens = sum(entries[m]["tokens"] for m in members) / float(len(members))
        clusters.append({
            "members": [entries[m]["row"] for m in members],
            "size": len(members),
            "avg_tokens": round(avg_tokens, 1),
            "min_lexical": round(min(s[2] for s in inside), 3),
            "max_lexical": round(max(s[2] for s in inside), 3),
            "min_structural": round(min(s[3] for s in inside), 3),
            "rank": round(len(members) * avg_tokens, 1),
        })
    clusters.sort(key=lambda c: -c["rank"])
    return {"clusters": clusters, "compared": len(entries)}
