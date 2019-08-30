#!/usr/bin/env python

import logging
import os
import time
import pytz
import datetime
from csv import DictWriter, QUOTE_ALL

import click
import requests
import yaml
from simple_salesforce import Salesforce


@click.command()
@click.option('--config-file', envvar='SFDC_CONFIG_FILE', type=click.Path(exists=True, dir_okay=False),
              default="settings.yml", help="Path to a configuration YAML file")
@click.option('--fetch-only', envvar='SFDC_FETCH_ONLY')
def run(config_file, fetch_only):
    """
    Main Entry Point for the utility, will provide a CLI friendly version of this application
    """
    fetcher = SalesforceFetcher(config_file)
    fetcher.fetch_all(fetch_only)


class SalesforceFetcher(object):
    """
    Class that encapsulates all the fetching logic for SalesForce.
    """

    def __init__(self, config_path):
        """
        Bootstrap a fetcher class
        :param config_path: Path to the configuration file to use for this instance
        """
        # Get settings
        with open(config_path, 'r') as f:
            self.settings = yaml.load(f)

        # Configure the logger
        log_level = (logging.WARN, logging.DEBUG)[self.settings['debug']]
        LOG_FORMAT = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        logger = logging.getLogger("salesforce-fetcher")
        logger.setLevel(log_level)

        ch = logging.StreamHandler()
        ch.setFormatter(LOG_FORMAT)
        logger.addHandler(ch)

        logger.debug("Logging is set to DEBUG level")
        # let's not output the password
        #logger.debug("Settings: %s" % self.settings)

        self.logger = logger
        self.salesforce = Salesforce(**self.settings['salesforce']['auth'])

        # Make sure output dir is created
        output_directory = self.settings['output_dir']
        if not os.path.exists(output_directory):
            os.makedirs(output_directory)

    def fetch_all(self, fetch_only):
        """
        Fetch any reports or queries, writing them out as files in the output_dir
        """
        queries = self.load_queries()
        for name, query in queries.items():
            if fetch_only and name != fetch_only:
              self.logger.info("'--fetch-only %s' specified. Skipping fetch of %s" % (fetch_only,name))
              continue
            self.fetch_soql_query(name, query)

        reports = self.settings['salesforce']['reports']
        for name, report_url in reports.items():
            if fetch_only and name != fetch_only:
              self.logger.info("'--fetch-only %s' specified. Skipping fetch of %s" % (fetch_only,name))
              continue
            self.fetch_report(name, report_url)

        if fetch_only:
            if fetch_only == 'contact_deletes':
                self.fetch_contact_deletes(days=3)
        else:
            self.fetch_contact_deletes(days=3)

        self.logger.info("Job Completed")

    def fetch_contact_deletes(self, days=3):
        """
        Fetches all deletes from Contact for X days
        :param days: Fetch deletes from this number of days to present
        :return:
        """
        path = self.create_output_path('contact_deletes')
        end = datetime.datetime.now(pytz.UTC)  # we need to use UTC as salesforce API requires this!
        records = self.salesforce.Contact.deleted(end - datetime.timedelta(days=days), end)
        data_list = records['deletedRecords']
        fieldnames = list(data_list[0].keys())
        with open(path, 'w') as f:
            writer = DictWriter(f, fieldnames=fieldnames, quoting=QUOTE_ALL)
            writer.writeheader()
            for delta_record in data_list:
                writer.writerow(delta_record)

    def fetch_report(self, name, report_url):
        """
        Fetches a single prebuilt Salesforce report via an HTTP request
        :param name: Name of the report to fetch
        :param report_url: Base URL for the report
        :return:
        """

        self.logger.info("Fetching report - %s" % name)
        sf_host = self.settings['salesforce']['host']
        url = "%s%s?view=d&snip&export=1&enc=UTF-8&xf=csv" % (sf_host, report_url)

        resp = requests.get(url,
                            headers=self.salesforce.headers,
                            cookies={'sid': self.salesforce.session_id},
                            stream=True)

        path = self.create_output_path(name)
        with open(path, 'w+') as f:
            # Write the full contents
            f.write(resp.text.replace("\"", ""))

            # Remove the Salesforce footer (last 7 lines)
            f.seek(0, os.SEEK_END)
            pos = f.tell() - 1

            count = 0
            while pos > 0 and count < 7:
                pos -= 1
                f.seek(pos, os.SEEK_SET)
                if f.read(1) == "\n":
                    count += 1

            # So long as we're not at the start of the file, delete all the characters ahead of this position
            if pos > 0:
                # preserve the last newline then truncate the file
                pos += 1
                f.seek(pos, os.SEEK_SET)
                f.truncate()

    def fetch_soql_query(self, name, query):
        self.logger.info("Executing %s" % name)
        self.logger.info("Query is: %s" % query)
        path = self.create_output_path(name)
        result = self.salesforce.query(query)
        self.logger.info("First result set received")
        batch = 0
        count = 0
        if result['records']:
            fieldnames = list(result['records'][0].keys())
            fieldnames.pop(0)  # get rid of attributes
            with open(path, 'w') as f:
                writer = DictWriter(f, fieldnames=fieldnames, quoting=QUOTE_ALL)
                writer.writeheader()

                while True:
                    batch += 1
                    for row in result['records']:
                        # each row has a strange attributes key we don't want
                        row.pop('attributes', None)
                        writer.writerow(row)
                        count += 1
                        if count % 100000 == 0:
                            self.logger.debug("%s rows fetched" % count)

                    # fetch next batch if we're not done else break out of loop
                    if not result['done']:
                        result = self.salesforce.query_more(result['nextRecordsUrl'], True)
                    else:
                        break

        else:
            self.logger.warn("No results returned for %s" % name)

    def create_output_path(self, name):
        output_dir = self.settings['output_dir']
        date = time.strftime("%Y-%m-%d")
        child_dir = os.path.join(output_dir, name, date)
        if not os.path.exists(child_dir):
            os.makedirs(child_dir)

        filename = "output.csv"
        file_path = os.path.join(child_dir, filename)
        self.logger.info("Writing output to %s" % file_path)
        return file_path

    def create_contacts_query(self, query_dir, updates_only=False):
        """
        The intention is to have Travis upload the "contact_fields.yaml" file
        to a bucket where it can be pulled down dynamically by this script
        and others (instead of having to rebuild the image on each change)
        """

        query = ''
        fields_file = os.path.join(query_dir, 'contact_fields.yaml')
        with open(fields_file, 'r') as stream:
            contact_fields = yaml.safe_load(stream)

        query = "SELECT "
        for field in contact_fields['fields']:
            query += field + ', '

        query = query[:-2] + " FROM Contact"
        if updates_only:
            query += " WHERE LastModifiedDate >= LAST_N_DAYS:3"
  
        return query

    def load_queries(self):
        """
        load queries from an external directory
        :return: a dict containing all the SOQL queries to be executed
        """
        queries = {}

        query_dir = self.settings['salesforce']['query_dir']
        for file in os.listdir(query_dir):
            if file == 'contacts.soql':
              queries['contacts'] = self.create_contacts_query(query_dir)
            elif file == 'contact_updates.soql':
              queries['contact_updates'] = self.create_contacts_query(query_dir, updates_only=True)
            elif file.endswith(".soql"):
                name, ext = os.path.splitext(file)
                query_file = os.path.join(query_dir, file)
                with open(query_file, 'r') as f:
                    queries[name] = f.read().strip().replace('\n', ' ')

        return queries


if __name__ == '__main__':
    run()
