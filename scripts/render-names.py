#!/usr/bin/env python3
"""Print the unique metadata.name values of the given kinds in a rendered multi-doc manifest (space separated)."""
import sys

import yaml

path, *kinds = sys.argv[1:]
names = []
for doc in yaml.safe_load_all(open(path)):
    if doc and doc.get("kind") in kinds and doc["metadata"]["name"] not in names:
        names.append(doc["metadata"]["name"])
print(" ".join(names))
