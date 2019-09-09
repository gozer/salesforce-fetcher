#!/usr/bin/env python3

import yaml

with open('queries/contact_fields.yaml', 'r') as stream:
  contact_fields = yaml.safe_load(stream)

#print(contact_fields)

query = "SELECT "
for field in contact_fields['fields']:
  query += (field + ', ')

query = query[:-2] + " FROM Contact"
print(query)

for bucket in contact_fields['buckets']:
  print(bucket['name'])
  for list in bucket['lists']:
    print("  "+list)
