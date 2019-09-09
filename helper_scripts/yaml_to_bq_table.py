#!/usr/bin/env python3

import yaml, sys

if not sys.argv[1]:
  print("please provide a yaml filename to parse")
  sys.exit(1)

with open(sys.argv[1], 'r') as stream:
  contact_fields = yaml.safe_load(stream)

#print(contact_fields)

query = "CREATE TABLE contact ("
for field_dict in contact_fields['fields']:
  for k, v in field_dict.items():
    query += (k + ' ' + v + ",\n")

query = query[:-2] + ')'
print(query)
