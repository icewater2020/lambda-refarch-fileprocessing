from datetime import datetime
import json
import logging
import os
import sys
import tempfile

import aws_lambda_logging
import boto3
import botocore


max_object_size = 104857600  # 100MB = 104857600 bytes

sentiment_table = os.getenv('SENTIMENT_TABLE')
sentiment_queue = os.getenv('SENTIMENT_QUEUE')

log_level = os.getenv('LOG_LEVEL')

comprehend_client = boto3.client('comprehend')

s3_resource = boto3.resource('s3')

dynamodb_resource = boto3.resource('dynamodb')
table = dynamodb_resource.Table(sentiment_table)

sqs_client = boto3.client('sqs')

log = logging.getLogger()


def check_s3_object_size(bucket, key_name):
    try:
        size = s3_resource.Object(bucket, key_name).content_length
    except Exception as e:
        print('Error: {}'.format(str(e)))
        size = 'NaN'

    return size


def get_s3_object(bucket, key_name, local_file):
    try:
        s3_resource.Bucket(bucket).download_file(key_name, local_file)
        return 'ok'
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] == '404':
            return 'Error: s3://{}/{} does not exist'.format(bucket, key_name)
        else:
            return 'Error: {}'.format(str(e))


def put_sentiment(s3_object, sentiment):
    try:
        response = table.put_item(
            Item={
                'id': s3_object,
                'last_modified': datetime.utcnow().isoformat(),
                'overall_sentiment': sentiment['Sentiment'],
                'positive': str(sentiment['SentimentScore']['Positive']),
                'negative': str(sentiment['SentimentScore']['Negative']),
                'neutral': str(sentiment['SentimentScore']['Neutral']),
                'mixed': str(sentiment['SentimentScore']['Mixed'])
            }
        )

        result = 'ok'

    except Exception as e:
        result = str(e)

    return result


def handler(event, context):
    aws_lambda_logging.setup(level=log_level,
                             aws_request_id=context.aws_request_id)

    for record in event['Records']:
        tmpdir = tempfile.mkdtemp()

        sqs_message_id = record['messageId']
        sqs_event_source_arn = record['eventSourceARN']

        sqs_receipt_handle = record['receiptHandle']

        try:
            json_body = json.loads(record['body'])
            request_params = json_body['detail']['requestParameters']
            bucket_name = request_params['bucketName']
            key_name = request_params['key']

            size = check_s3_object_size(bucket_name, key_name)

            if size >= max_object_size:
                log.error('''Source S3 object s3://{}/{} is larger ({} bytes)
                than {} max object bytes'''.format(
                               bucket_name,
                               key_name,
                               size,
                               max_object_size))
                raise Exception("Source S3 object too large")

            local_file = os.path.join(tmpdir, key_name)

            download_status = get_s3_object(bucket_name, key_name, local_file)

            if download_status == 'ok':
                key_bytes = os.stat(local_file).st_size
                src_s3_download_bytes = key_bytes
                log.info('''Download to {} for sentiment analysis'''.format(
                    local_file
                    ))
            else:
                raise Exception("Download failure to {}".format(local_file))

            md_contents = open(local_file, 'r').read()

            sentiment = comprehend_client.detect_sentiment(
                Text=md_contents,
                LanguageCode='en'
            )

            log.info('Overall sentiment: {} ({})'.format(
                sentiment['Sentiment'],
                sentiment['SentimentScore']
            ))

            source_s3_object = 's3://{}/{}'.format(bucket_name, key_name)

            put_sentiment_result = put_sentiment(source_s3_object, sentiment)

            if put_sentiment_result == 'ok':
                '''If function could put the sentiment to the DDB table then remove message
                from SQS queue.'''
                try:
                    sqs_client.delete_message(
                        QueueUrl=sentiment_queue,
                        ReceiptHandle=sqs_receipt_handle
                    )
                except Exception as e:
                    raise Exception(str(e))

                log.info('Put sentiment of {} to table {}'.format(
                    local_file, sentiment_table))

        except Exception as e:
            raise Exception("Could not get sentiment: {}".format(str(e)))
            return 'fail'

        finally:
            filesToRemove = os.listdir(tmpdir)

            for f in filesToRemove:
                file_path = os.path.join(tmpdir, f)
                print(f'Removing File: {file_path}')

                try:
                    os.remove(file_path)
                except OSError as e:
                    print(e)
                    print(f'Error while deleting file {file_path}')

            print(f'Removing Folder: {tmpdir}')
            os.rmdir(tmpdir)

        return('ok')
