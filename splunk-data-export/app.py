""" Lambda function designed to export data from a Splunk index using SPL. """

## Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
## SPDX-License-Identifier: MIT-0

# pylint: disable=import-error
import json
import os
import logging
import time
from datetime import datetime
from socket import gaierror
import pytz
import boto3
import botocore
import pandas as pd
from splunklib import client
from splunklib import results

# Set logging for this function.
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment specific parameters pulled from AWS Lambda environment variables.
AWS_REGION = os.environ["AWS_REGION"]
AWS_SECRETS_MANAGER_SPLUNK_SECRET_NAME = os.environ["AWS_SECRETS_MANAGER_SPLUNK_SECRET_NAME"]
AMAZON_S3_DATA_EXPORT_BUCKET_NAME = os.environ["AMAZON_S3_DATA_EXPORT_BUCKET_NAME"]
AMAZON_S3_DATA_EXPORT_TOP_LEVEL_PATH = os.environ["AMAZON_S3_DATA_EXPORT_TOP_LEVEL_PATH"]
DATA_EXPORT_CONFIGURATION_FILE = os.environ["DATA_EXPORT_CONFIGURATION_FILE"]

# Create an AWS Secrets Manager client and get secret value.
get_secret_value_response = {}
try:
    session = boto3.session.Session()
    sm_client = session.client(
            service_name="secretsmanager",
            region_name=AWS_REGION
        )
    get_secret_value_response = sm_client.get_secret_value(
            SecretId=AWS_SECRETS_MANAGER_SPLUNK_SECRET_NAME
        )
except botocore.exceptions.ClientError as sm_error:
    logger.error("Error encountered: %s", str(sm_error), exc_info=True)
# Get Splunk bearer token.
SPLUNK_BEARER_TOKEN = json.loads(get_secret_value_response["SecretString"])["SplunkBearerToken"]
# Get Splunk URL.
SPLUNK_DEPLOYMENT_NAME = json.loads(
        get_secret_value_response["SecretString"]
    )["SplunkDeploymentName"]
SPLUNK_HOST = SPLUNK_DEPLOYMENT_NAME+".splunkcloud.com"
# Get Splunk index.
SPLUNK_INDEX = json.loads(get_secret_value_response["SecretString"])["SplunkIndexName"]
SPLUNK_PORT = 8089
# Create a Splunk service instance and log in.
service = client.connect(
        host=SPLUNK_HOST,
        port=SPLUNK_PORT,
        splunkToken=SPLUNK_BEARER_TOKEN,
        autologin=True
    )
# Check access to an index to verify connection.
try:
    indexes = service.indexes.list()
except gaierror as gai_error:
    logger.error("Error encountered: %s", str(gai_error), exc_info=True)
else:
    index_names = []
    for index in indexes:
        index_names.append(index.name)
    # Check if selected index is found.
    if SPLUNK_INDEX not in index_names:
        logger.error("Splunk index %s not found.", SPLUNK_INDEX)
    else:
        logger.info("Splunk index %s found.", SPLUNK_INDEX)
# Read configuration file.
with open(DATA_EXPORT_CONFIGURATION_FILE, encoding="utf-8") as json_configuration_file:
    configuration = json.load(json_configuration_file)

def _convert_time_to_utc(time_with_offset):
    """ Converts internal _time field from Splunk into UTC string.
    Example input: 2024-04-08T03:49:18.123+01:00
    Example output: 2024-04-08T02:49:18.123Z
    """
    return datetime.strptime(time_with_offset, "%Y-%m-%dT%H:%M:%S.%f%z").\
        astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

def save_to_s3_bucket(s3, items):
    """ Commits results from SPL to S3 bucket. """
    # Load results into pandas dataframe.
    df = pd.DataFrame(items)
    logger.info("Columns identified: %s", str(df.dtypes.to_dict()))
    # Convert Splunk _time offset time format to UTC
    # time format which can be processed by AWS Glue.
    df["_time"] = df["_time"].apply(_convert_time_to_utc)
    # Convert columns to the best possible dtypes.
    df = df.convert_dtypes().apply(pd.to_numeric, errors="ignore")
    # Explicitly specify _time as datetime.
    df["_time"] = pd.to_datetime(df["_time"])
    # Duplicate partition columns so that they are retained in output file,
    # not just in partition folder structure. Required for services that
    # ignore data stored in partitions.
    for col_index, partition_column in enumerate(s3["partition_cols"]):
        df[s3["partition_cols_duplicate"][col_index]] = df[partition_column]
    logger.info("DataFrame info: %s", str(df.info(memory_usage="deep")))
    logger.info("Columns to be written: %s", str(df.dtypes.to_dict()))
    # Store Splunk data in Parquet format.
    s3_path = "s3://" + AMAZON_S3_DATA_EXPORT_BUCKET_NAME + \
              "/" + AMAZON_S3_DATA_EXPORT_TOP_LEVEL_PATH + "/" + s3["path"]
    logger.info("Uploading data to %s...", str(s3_path))
    try:
        df.to_parquet(
                s3_path,
                compression=s3["compression"], index=False,
                engine=s3["engine"],
                partition_cols=s3["partition_cols"],
                existing_data_behavior="delete_matching",
                use_deprecated_int96_timestamps=True
            )
    except OSError as os_error:
        logger.error("Error encountered: %s", str(os_error), exc_info=True)
    else:
        logger.info("Data upload complete.")

def export_data(s3, spl):
    """ Runs SPL against Splunk index and returns results. """
    kwargs_export = spl["search_export"]
    search_query = spl["search_query"]
    start_time = time.time()
    job = service.jobs.create(search_query)
    logger.info("Started search job (job: %s)...", str(job.name))
    # Poll for job completion.
    while not job.is_done():
        time.sleep(1)
    result_count = job["resultCount"]
    logger.info("Search job has results count of %s.", str(result_count))
    offset = 0
    job_results_items = []
    # Use pagination to retrieve results in sets.
    while offset < int(result_count):
        kwargs_export["offset"] = offset
        job_results = results.JSONResultsReader(job.results(**kwargs_export))
        logger.info("Search offset %s.", str(offset))
        for result in job_results:
            if isinstance(result, results.Message):
                # Diagnostic messages may be returned in the results.
                logger.info("%s: %s", result.type, result.message)
            elif isinstance(result, dict):
                # Normal events are returned as dicts.
                job_results_items.append(result)
        offset += int(kwargs_export["count"])
    logger.info("Search job completed (job: %s).", str(job.name))
    duration = int(time.time()-start_time)
    logger.info(
            "%s records returned in %ss (job: % seconds).", str(len(job_results_items)),
            str(int(duration)), str(job.name)
        )
    if len(job_results_items) > 0:
        save_to_s3_bucket(s3, job_results_items)
    return len(job_results_items)

# pylint: disable=unused-argument
def lambda_handler(event, context):
    """ Main Lambda function handler code. """
    # Run a search and export data to an Amazon S3 bucket.
    logger.info(
            "Splunk data export started (target S3 bucket: %s)...",
            AMAZON_S3_DATA_EXPORT_BUCKET_NAME
        )
    for search in configuration["searches"]:
        export_data(search["s3"], search["spl"])
    logger.info("Splunk data export completed.")
