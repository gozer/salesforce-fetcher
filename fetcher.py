#!/usr/bin/env python

import collections
import json
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
from salesforce_bulk import SalesforceBulk

import multiprocessing as mp

try:
    import urlparse
except ImportError:
    import urllib.parse as urlparse

@click.command()
@click.option('--config-file', envvar='SFDC_CONFIG_FILE', type=click.Path(exists=True, dir_okay=False),
              default="settings.yml", help="Path to a configuration YAML file")
@click.option('--fetch-only', envvar='SFDC_FETCH_ONLY')
@click.option('--airflow-date', envvar='SFDC_AIRFLOW_DATE')
@click.option('--fetch-method', envvar='SFDC_FETCH_METHOD')
@click.option('--days-lookback', envvar='SFDC_DAYS_LOOKBACK')
def run(config_file, fetch_only, airflow_date, fetch_method, days_lookback=29):
    """
    Main Entry Point for the utility, will provide a CLI friendly version of this application
    """
    fetcher = SalesforceFetcher(config_file)
    if days_lookback is None:
        days_lookback_int = 29
    else:
        days_lookback_int = int(days_lookback)
    #logger.debug("Days Lookback: " + str(days_lookback_int))
    fetcher.fetch_all(fetch_only, airflow_date, fetch_method, days_lookback_int)


def get_and_write_bulk_results(batch_id, result_id, job, endpoint, headers, path):
    with open(path, 'wb') as f:
        uri = urlparse.urljoin(
            endpoint + "/",
            "job/{0}/batch/{1}/result/{2}".format(
                job, batch_id, result_id),
        )
        resp = requests.get(uri, headers=headers, stream=True)

        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                f.write( chunk )

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
            self.settings = yaml.safe_load(f)

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
        self.salesforce_bulk = SalesforceBulk(**self.settings['salesforce']['auth'], API_version='46.0')

        # Make sure output dir is created
        output_directory = self.settings['output_dir']
        if not os.path.exists(output_directory):
            os.makedirs(output_directory)

    def fetch_all(self, fetch_only, airflow_date, fetch_method, days_lookback):
        """
        Fetch any reports or queries, writing them out as files in the output_dir
        """
        queries = self.load_queries()
        for name, query in queries.items():
            if fetch_only and name != fetch_only:
              self.logger.debug("'--fetch-only %s' specified. Skipping fetch of %s" % (fetch_only,name))
              continue
            #if name == 'contacts' or name == 'opportunity':
            if fetch_method and fetch_method == 'bulk':
                self.fetch_soql_query_bulk(name, query, airflow_date)
            else:
                self.fetch_soql_query(name, query, airflow_date)

        reports = self.settings['salesforce']['reports']
        for name, report_url in reports.items():
            if fetch_only and name != fetch_only:
              self.logger.debug("'--fetch-only %s' specified. Skipping fetch of %s" % (fetch_only,name))
              continue
            self.fetch_report(name, report_url, airflow_date)

        if fetch_only:
            if fetch_only == 'contact_deletes':
                self.fetch_contact_deletes(days=days_lookback, airflow_date=airflow_date)
        else:
            self.fetch_contact_deletes(days=days_lookback, airflow_date=airflow_date)

        self.logger.info("Job Completed")

    def fetch_contact_deletes(self, days=29, airflow_date=None):
        """
        Fetches all deletes from Contact for X days
        :param days: Fetch deletes from this number of days to present
        :return:
        """
        path = self.create_output_path('contact_deletes', airflow_date=airflow_date)
        end = datetime.datetime.now(pytz.UTC)  # we need to use UTC as salesforce API requires this!
        records = self.salesforce.Contact.deleted(end - datetime.timedelta(days=days), end)
        data_list = records['deletedRecords']
        if len(data_list) > 0:
            fieldnames = list(data_list[0].keys())
            with open(path, 'w') as f:
                writer = DictWriter(f, fieldnames=fieldnames, quoting=QUOTE_ALL)
                writer.writeheader()
                for delta_record in data_list:
                    writer.writerow(delta_record)

    def fetch_report(self, name, report_url, airflow_date=None):
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

        path = self.create_output_path(name, airflow_date=airflow_date)
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

    def fetch_soql_query_bulk(self, name, query, airflow_date=None):
        self.logger.info("BULK Executing %s" % name)
        self.logger.info("BULK Query is: %s" % query)
        if name == 'contacts' or name == 'contact_updates':
            table_name = 'Contact'
        elif name == 'opportunity' or name == 'opportunity_updates':
            table_name = 'Opportunity'
        job = self.salesforce_bulk.create_query_job(table_name,
                                                    contentType='CSV',
                                                    pk_chunking=True,
                                                    concurrency='Parallel')
        self.logger.info("job: %s" % job)
        batch = self.salesforce_bulk.query(job, query)
#        job = '7504O00000LUxuCQAT'
#        batch = '7514O00000TvapeQAB'
        self.logger.info("Bulk batch created: %s" % batch)

        while True:
            batch_state = self.salesforce_bulk.batch_state(batch, job_id=job, reload=True).lower()
            if batch_state == 'notprocessed':
                self.logger.info("master batch is done")
                break
            elif batch_state == 'aborted' or batch_state == 'failed':
                self.logger.error("master batch failed")
                self.logger.error(self.salesforce_bulk.batch_status(batch_id=batch, job_id=job, reload=True))
                raise Exception("master batch failed")
            self.logger.info("waiting for batch to be done")
            time.sleep(10)

        count = 0
        downloaded = {}

        pool = mp.Pool(5)

        while True:
            stats = {}
            batch_count = 0
            all_batches = self.salesforce_bulk.get_batch_list(job)
            for batch_info in all_batches:
                batch_count += 1

                batch_state = batch_info['state'].lower()
                if batch_state in stats:
                    stats[batch_state] += 1
                else:
                    stats[batch_state] = 1

                if batch_info['id'] == batch:
                    #self.logger.debug("skipping the master batch id")
                    continue
                elif batch_info['id'] in downloaded:
                    #self.logger.debug("batch %s already downloaded" % batch_info['id'])
                    continue
 
                if batch_state == 'completed':
                    self.logger.debug("batch %s (%s of %s)" % (batch_info['id'],
                                                               batch_count,
                                                               len(all_batches)))
    
                    for result_id in self.salesforce_bulk.get_query_batch_result_ids(batch_info['id'], job_id=job):
                        self.logger.debug("result_id: %s" % result_id)
                        path = self.create_output_path(name, result_id, airflow_date=airflow_date)
                        pool.apply_async( get_and_write_bulk_results,
                                          args=(batch_info['id'],
                                                result_id,
                                                job,
                                                self.salesforce_bulk.endpoint,
                                                self.salesforce_bulk.headers(),
                                                path)
                        )

                    downloaded[batch_info['id']] = 1

                elif batch_state == 'failed':
                    downloaded[batch_info['id']] = 1
                    self.logger.error("batch %s failed!" % batch_info['id'])
                    self.logger.error(self.salesforce_bulk.batch_status(batch_id=batch_info['id'], job_id=job, reload=True))
            
            if 'completed' in stats and stats['completed'] + 1 == batch_count:
                self.logger.info("all batches retrieved")
                break
            elif 'failed' in stats and stats['failed'] + 1 == batch_count:
                self.logger.error("NO batches retrieved")
                self.logger.error(self.salesforce_bulk.batch_status(batch_id=batch, job_id=job, reload=True))
                raise Exception("NO batches retrieved")
            elif 'failed' in stats and stats['failed'] + stats['completed'] == batch_count:
                self.logger.warning("all batches WITH SOME FAILURES")
                break
            else:
                self.logger.info(stats)
                time.sleep(5)

        try:
            self.salesforce_bulk.close_job(job)
        except:
            pass
        pool.close()
        pool.join()


    def fetch_soql_query(self, name, query, airflow_date=None):
        self.logger.info("Executing %s" % name)
        self.logger.info("Query is: %s" % query)
        path = self.create_output_path(name, airflow_date=airflow_date)
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
                        out_dict = {}
                        for key, value in row.items():
                            if type(value) is collections.OrderedDict:
                                out_dict[key] = json.dumps(value)
                            else:
                                out_dict[key] = value
                        writer.writerow(out_dict)
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

    def create_output_path(self, name, filename='output', airflow_date=None):
        output_dir = self.settings['output_dir']
        if airflow_date:
            date = airflow_date
        else:
            date = time.strftime("%Y-%m-%d")
        child_dir = os.path.join(output_dir, name, date)
        if not os.path.exists(child_dir):
            os.makedirs(child_dir)

        filename = filename+".csv"
        file_path = os.path.join(child_dir, filename)
        self.logger.info("Writing output to %s" % file_path)
        return file_path

    def create_custom_query(self, table_name='Contact', dir='/usr/local/salesforce_fetcher/queries', updates_only=False):
        """
        The intention is to have Travis upload the "contact_fields.yaml" file
        to a bucket where it can be pulled down dynamically by this script
        and others (instead of having to rebuild the image on each change)
        """

        fields_file_name = table_name.lower() + '_fields.yaml'
        fields_file = os.path.join(dir, fields_file_name)
        if not os.path.exists(fields_file):
            return
        with open(fields_file, 'r') as stream:
            columns = yaml.safe_load(stream)

        query = "SELECT "
        for field in columns['fields']:
            query += next(iter(field)) + ', '

        query = query[:-2] + " FROM " + table_name
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
            if file.endswith(".soql"):
                name, ext = os.path.splitext(file)
                query_file = os.path.join(query_dir, file)
                with open(query_file, 'r') as f:
                    queries[name] = f.read().strip().replace('\n', ' ')

        # explicitly add the non-file queries
        queries['contacts'] = self.create_custom_query(table_name='Contact',
                                                       dir=query_dir)
        queries['contact_updates'] = self.create_custom_query(table_name='Contact',
                                                              dir=query_dir,
                                                              updates_only=True)
        queries['opportunity'] = self.create_custom_query(table_name='Opportunity',
                                                          dir=query_dir)
        queries['opportunity_updates'] = self.create_custom_query(table_name='Opportunity',
                                                                  dir=query_dir,
                                                                  updates_only=True)

        return queries


if __name__ == '__main__':
    run()
