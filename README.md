# Salesforce Fetcher

CLI tool for fetching queries and reports from salesforce using `simple-salesforce`
and `requests` libraries

Configuration is managed via a yaml file matching the basic layout of:

```yaml
---
debug: True
output_dir: output
salesforce:
  auth:
    username:
    password:
    security_token:
    sandbox: false

  host: https://hostname.my.salesforce.com/\
  query_dir: queries
  reports:
    report_name: report_id
```