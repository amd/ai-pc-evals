"""Extract the agent's final answer from a copied Perplexity thread (stdin) to stdout.

The thread copy is: prompt, 'Local agent: ...', the agent's time, the answer, then 'Sources' / 'Usage'.
"""
import re
import sys

text = sys.stdin.read().replace("\r\n", "\n")
m = re.search(r"\n(?:\d+m )?\d+s\n\n(.*?)\n\n+Sources\n", text, re.S)
if not m:
    sys.exit("ERROR: could not find the answer between the agent time and 'Sources'")
print(m.group(1).strip())
