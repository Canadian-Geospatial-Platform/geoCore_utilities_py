import os
import json
import boto3
import requests
import datetime

from uuid import UUID
from botocore.exceptions import ClientError

GEOJSON_BUCKET_NAME        = os.environ['GEOJSON_BUCKET_NAME']
GEONETWORK_POPULARITY_PATH = os.environ['GEONETWORK_POPULARITY_PATH']
DYNAMODB_TABLE             = os.environ['DYNAMODB_TABLE']
region                     = "ca-central-1"

"""SAMPLE JSON TEST
    {
        "queryStringParameters":
        {
            "crud":"create_all",
            
            "_crud":"create",
            "_uuid":SOME_UUID,
            "_pop":SOME_POPULARITY,
            
            "__crud":"update",
            "__uuid":SOME_UUID,
            "__pop":SOME_POPULARITY,
            
            "___crud":"read_all",
            
            "____crud":"read",
            "____uuid":SOME_UUID,
            
            "_____crud":"delete",
            "_____uuid":SOME_UUID
        }
    }
"""

def lambda_handler(event, context):
    
    """PROD SETTINGS"""
    
    s3_paginate_options = {'Bucket': GEOJSON_BUCKET_NAME} # Python dict, seperate with a comma: {'StartAfter'=2018,'Bucket'='demo'} see: https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html#S3.Client.list_objects_v2
    message = ""

    """ 
    Parse query string parameters 
    """
    try:
        crud = event['queryStringParameters']['crud']
    except:
        crud = None
        
    try:
        uuid = event['queryStringParameters']['uuid']
    except:
        uuid = None
    
    try:
        pop = event['queryStringParameters']['pop']
    except:
        pop = None        
    
    operation = parse_query_parameters(crud, uuid, pop)
    
    """ 
    Perform requested operation
    """
    if operation == "create_all":
        dynamodb = boto3.resource('dynamodb', region_name=region)
        popularity_table = DYNAMODB_TABLE
        
        #delete existing table if exist
        try:
            client = boto3.client('dynamodb')
            delete_uuid_popularity_table(popularity_table, dynamodb)
            waiter = client.get_waiter('table_not_exists')
            waiter.wait(TableName=popularity_table)
        except ClientError as e:
            print(e)
        
        #create table
        try:  
            client = boto3.client('dynamodb')
            pop_table = create_uuid_popularity_table(popularity_table, dynamodb)
            waiter = client.get_waiter('table_exists')
            waiter.wait(TableName=popularity_table)
            print("Table status:", pop_table.table_status)
        except ClientError as e:
            print(e)
        
        
        #list all files in the s3 bucket
        try:
            filename_list = s3_filenames_paginated(region, **s3_paginate_options)
        except ClientError as e:
            print("Could not paginate the geojson bucket: %s" % e)
            
        #create new entries in dynamodb
        count = 0
        for uuid in filename_list:
            #AWS Lambda cannot process all 5000+ records within 15 minutes
            #Hence, here is a crude pagination based on first HEX character (0-F) of the UUID. 
            #if uuid[0] <= '8':
            if uuid[0] > '8':
                uuid = uuid.replace('.geojson', '')
                
                #get popularity
                url = GEONETWORK_POPULARITY_PATH + uuid
                response = requests.get(GEONETWORK_POPULARITY_PATH + uuid)
                
                str_data = json.loads(response.text)
                popularity = int(str_data['popularity'])
                
                #put popularity into dynamodb
                put_uuid_popularity(uuid, popularity, popularity_table, dynamodb)
                print("UUID: ", uuid , "with popularity: " , str(popularity) , " was inserted into table " , popularity_table)
                count += 1
        
        message += count + " records inserted using 'create_all' parameter"
    
    elif operation == "create":
        pass
    elif operation == "update":
        pass
    elif operation == "read_all":
        pass
    elif operation == "read":
        pass
    elif operation == "delete":
        pass
    
    return {
        'statusCode': 200,
        'body': json.dumps(message)
    }
    
def create_uuid_popularity_table(popularity_table, dynamodb=None):
    if not dynamodb:
        dynamodb = boto3.resource('dynamodb', region_name=region)

    table = dynamodb.create_table(
        TableName=popularity_table,
        KeySchema=[
            {
                'AttributeName': 'uuid',
                'KeyType': 'HASH'  # Partition key
            },
            {
                'AttributeName': 'popularity',
                'KeyType': 'RANGE'  # Sort key
            }
        ],
        AttributeDefinitions=[
            {
                'AttributeName': 'uuid',
                'AttributeType': 'S'
            },
            {
                'AttributeName': 'popularity',
                'AttributeType': 'N'
            }
        ],
        BillingMode='PAY_PER_REQUEST',
    )
    print(popularity_table, " table created.")
    return table

def put_uuid_popularity(uuid, popularity, popularity_table, dynamodb=None):
    if not dynamodb:
        dynamodb = boto3.resource('dynamodb', region_name=region)
        
    dateTime = datetime.datetime.utcnow().now()
    dateTime = dateTime.isoformat()[:-7] + 'Z'
    
    table = dynamodb.Table(popularity_table)
    response = table.put_item(
       Item={
            'uuid': uuid,
            'popularity': popularity,
            'datetime': dateTime
        }
    )
    return response

def delete_uuid_popularity_table(popularity_table, dynamodb=None):
    if not dynamodb:
        dynamodb = boto3.resource('dynamodb', region_name=region)

    table = dynamodb.Table(popularity_table)
    table.delete()
    print(popularity_table, " table deleted.")
    
def parse_query_parameters(crud, uuid, pop):
    """ 
    Determines the operation of the lambda using duck typing. Cleans the incoming values
    :param   crud: Create, Read, Update or Delete (CRUD) operation
             uuid: UUID v4
             pop:  popularity (integer)
    :return: operation used for this lambda invocation
    """
    try:
        #prevent param attacks
        if (len(crud) > 10):
            return "read_all"
        
        #no additional checks needed
        if crud == "create_all" or crud == "read_all":
            return crud

        #if 'read' or 'delete', must provide uuid
        try:
            if crud == "read" and is_valid_uuid(uuid):
                return "read"
            elif crud == "delete" and is_valid_uuid(uuid):
                return "delete"
        except:
            print("Invalid ", crud , " format. Please check documentation.")
            return "read_all"
            
        #if 'create' or 'update', must provide uuid and pop information
        try:
            if crud == "create" and is_valid_uuid(uuid) and int(pop) >= 0:
                return "create"
            elif crud == "update" and is_valid_uuid(uuid) and int(pop) >= 0:
                return "update"
        except:
            print("Invalid ", crud , " format. Please check documentation.")
            return "read_all"
    except:
        return "read_all"

def s3_filenames_paginated(region, **kwargs):
    """
    Paginates a S3 bucket to obtain file names. Pagination is needed as S3 returns 999 objects per request (hard limitation)
    :param region: region of the s3 bucket 
    :param kwargs: Must have the bucket name. For other options see the list_objects_v2 paginator: 
    :              https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html#S3.Client.list_objects_v2
    :return: a list of filenames within the bucket
    """
    client = boto3.client('s3', region_name=region)
    
    paginator = client.get_paginator('list_objects_v2')
    result = paginator.paginate(**kwargs)
    
    filename_list = []
    count = 0
    
    for page in result:
        if "Contents" in page:
            for key in page[ "Contents" ]:
                keyString = key[ "Key" ]
                #print(keyString)
                count += 1
                filename_list.append(keyString)
    
    print("Bucket contains:", count, "files")
                
    return filename_list

def is_valid_uuid(uuid, version=4):
    """ 
    Checks if a universal unique ID (i.e., UUID) is valid 
    :param uuid: input UUID
    :version verion of uuid: see https://docs.python.org/3/library/uuid.html
    :return: True if UUID is valid and False otherwise
    """
    try:
        uuid_obj = UUID(uuid, version=version)
        return True
    except ValueError:
        return False