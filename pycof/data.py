import os
import sys
import getpass
import boto3
from botocore.exceptions import ProfileNotFound

import pandas as pd
import numpy as np
import math
import json
import xlrd
import hashlib
from io import StringIO, BytesIO
import urllib.request
from types import SimpleNamespace

from warnings import warn

import re

from tqdm import tqdm
import datetime

from .misc import write, _get_config, file_age, verbose_display, _pycof_folders


##############################################################################################################################

# Easy file read
def read(path, extension=None, parse=True, remove_comments=True, sep=',', sheet_name=0, engine='auto', credentials={}, profile_name=None, cache='30mins', cache_name=None, verbose=False, **kwargs):
    """Read and parse a data file.
    It can read multiple format. For data frame-like format, the function will return a pandas data frame, otherzise a string.
    The function will by default detect the extension from the file path. You can force an extension with the argument.
    It can remove comments, trailing spaces, breaklines and tabs. It can also replace f-strings with provided values.

    :Parameters:
        * **path** (:obj:`str`): path to the SQL file.
        * **extension** (:obj:`str`): extension to use. Can be 'csv', 'txt', 'xslsx', 'sql', 'html', 'py', 'json', 'js', 'parquet', 'read-only' (defaults None).
        * **parse** (:obj:`bool`): Format the query to remove trailing space and comments, ready to use format (defaults True).
        * **remove_comments** (:obj:`bool`): Remove comments from the loaded file (defaults True).
        * **sep** (:obj:`str`): Columns delimiter for pd.read_csv (defaults ',').
        * **sheet_name** (:obj:`str`): Tab column to load when reading Excel files (defaults 0).
        * **engine** (:obj:`str`): Engine to use to load the file. Can be 'pyarrow' or the function from your preferred library (defaults 'auto').
        * **credentials** (:obj:`dict`): Credentials to use to connect to AWS S3. You can also provide the credentials path or the json file name from '/etc/.pycof' (defaults {}).
        * **profile_name** (:obj:`str`): Profile name of the AWS profile configured with the command `aws configure` (defaults None).
        * **cache** (:obj:`str`): Caches the data to avoid downloading again.
        * **cache_name** (:obj:`str`): File name for storing cache data, if None the name will be generated by hashing the path (defaults None).
        * **verbose** (:obj:`bool`): Display intermediate steps (defaults False).
        * **\\*\\*kwargs** (:obj:`str`): Arguments to be passed to the engine or values to be formated in the file to load.

    :Configuration:
        The function requires the below arguments in the configuration file.
        
        * :obj:`AWS_ACCESS_KEY_ID`: AWS access key, can remain empty if an IAM role is assign to the host.
        * :obj:`AWS_SECRET_ACCESS_KEY`: AWS secret key, can remain empty if an IAM role is assign to the host.
        
        .. code-block:: python

            {
            "AWS_ACCESS_KEY_ID": "",
            "AWS_SECRET_ACCESS_KEY": ""
            }

    :Example:
        >>> sql = pycof.read('/path/to/file.sql', country='FR')
        >>> df1 = pycof.read('/path/to/df_file.json')
        >>> df2 = pycof.read('/path/to/df.csv')
        >>> df3 = pycof.read('s3://bucket/path/to/file.parquet')
    :Returns:
        * :obj:`pandas.DataFrame`: Data frame a string from file read.
    """
    # Initialize ext var
    ext = path.split('.')[-1] if extension is None else extension
    # Initialize orgn var
    if path.startswith('s3://'):
        orgn = 'S3'
    elif path.startswith('http'):
        orgn = 'http'
    else:
        orgn = 'other'
    # Initialize data var
    data = []

    if orgn == 'S3':
        try:
            sess = boto3.session.Session(profile_name=profile_name)
            s3 = sess.client('s3')
            s3_resource = sess.resource('s3')
        except ProfileNotFound:
            config = _get_config(credentials)
            s3 = boto3.client('s3', aws_access_key_id=config.get("AWS_ACCESS_KEY_ID"), aws_secret_access_key=config.get("AWS_SECRET_ACCESS_KEY"),
                            region_name=config.get("REGION"))
            s3_resource = boto3.resource('s3', aws_access_key_id=config.get("AWS_ACCESS_KEY_ID"),
                                        aws_secret_access_key=config.get("AWS_SECRET_ACCESS_KEY"),
                                        region_name=config.get("REGION"))
        except FileNotFoundError:
            raise ConnectionError("Please run 'aws config' on your terminal and initialize the parameters or profide a correct value for crendetials.")

        bucket = path.replace('s3://', '').split('/')[0]
        folder_path = '/'.join(path.replace('s3://', '').split('/')[1:])

        if ext.lower() in ['csv', 'txt', 'parq', 'parquet', 'fea', 'feather', 'html', 'json', 'js', 'py', 'sh', 'xls', 'xlsx']:
            # If file can be loaded by pandas, we do not download locally
            verbose_display('Loading the data from S3 directly', verbose)
            obj = s3.get_object(Bucket=bucket, Key=folder_path)
            path = BytesIO(obj['Body'].read())
        else:
            # This step will only check the cache and download the file to tmp if not available.
            # The normal below steps will still run, only the path will change if the file comes from S3
            # and cannot be loaded by pandas.

            cache_time = 0. if cache is False else cache
            _disp = tqdm if verbose else list
            # Force the input to be a string
            str_c_time = str(cache_time).lower().replace(' ', '')
            # Get the numerical part of the input
            c_time = float(''.join(re.findall('[^a-z]', str_c_time)))
            # Get the str part of the input - for the format
            age_fmt = ''.join(re.findall('[a-z]', str_c_time))

            # Hash the path to create filename
            file_name = cache_name if cache_name else hashlib.sha224(bytes(path, 'utf-8')).hexdigest().replace('-', 'm')
            data_path = _pycof_folders('data')

            # Changing path to local once file is downloaded to tmp folder
            path = os.path.join(data_path, file_name)

            # Set the S3 bucket
            s3bucket = s3_resource.Bucket(bucket)

            # First, check if the same path has already been downloaded locally
            if file_name in os.listdir(data_path):
                # If yes, check when and compare to cache time
                if file_age(path, format=age_fmt) < c_time:
                    # If cache is recent, no need to download
                    ext = os.listdir(path)[0].split('.')[-1]
                    verbose_display('Data file available in cache', verbose)
                else:
                    # Otherwise, we update the cache
                    verbose_display('Updating data in cache', verbose)
                    # Remove the existing the content of the existing folder before downloading the updated data
                    for root, _, files in os.walk(path):
                        for name in files:
                            os.remove(os.path.join(root, name))
                    # Downloading the objects from S3
                    for obj in _disp(s3bucket.objects.filter(Prefix=folder_path)):
                        if (obj.key == folder_path) or (not any(e in obj.key for e in ['.parquet', '.parq', '.feather', '.fea', '.csv', '.json', '.txt'])):
                            continue
                        else:
                            s3bucket.download_file(obj.key, os.path.join(path, obj.key.split('/')[-1]))
                            ext = obj.key.split('.')[-1]
            else:
                # If the file is not in the cache, we download it
                verbose_display('Downloading and caching data', verbose)
                # Creating the directory
                os.makedirs(path, exist_ok=True)
                for obj in _disp(s3bucket.objects.filter(Prefix=folder_path)):
                    if obj.key == folder_path:
                        continue
                    s3bucket.download_file(obj.key, os.path.join(path, obj.key.split('/')[-1]))
                    ext = obj.key.split('.')[-1]

    # CSV / txt
    if ext.lower() in ['csv', 'txt']:
        data = pd.read_csv(path, sep=sep, **kwargs)
    # XLSX
    elif ext.lower() in ['xls', 'xlsx']:
        _engine = 'openpyxl' if engine == 'auto' else engine
        data = pd.read_excel(path, sheet_name=sheet_name, engine=_engine, **kwargs)
    # SQL
    elif ext.lower() in ['sql']:
        if type(path) == BytesIO:
            file = path.read().decode()
        else:
            with open(path) as f:
                file = f.read()
        for line in file.split('\n'):  # Parse the data
            l_striped = line.strip()  # Removing trailing spaces
            if parse:
                l_striped = l_striped.format(**kwargs)  # Formating
            if remove_comments:
                l_striped = l_striped.split('--')[0]  # Remove comments
                re.sub(r"<!--(.|\s|\n)*?-->", "", l_striped.replace('/*', '<!--').replace('*/', '-->'))
            if l_striped != '':
                data += [l_striped]
        data = ' '.join(data)
    # HTML
    elif ext.lower() in ['html']:
        if type(path) == BytesIO:
            file = path.read().decode()
        elif orgn == 'http':
            weburl = urllib.request.urlopen(path)
            file = weburl.read().decode("utf-8")
        else:
            with open(path) as f:
                file = f.read()

        # Parse the data
        for line in file.split('\n'):
            l_striped = line.strip()  # Removing trailing spaces
            if parse:
                l_striped = l_striped.format(**kwargs)  # Formating
            if remove_comments:
                l_striped = re.sub(r"<!--(.|\s|\n)*?-->", "", l_striped)  # Remove comments
            if l_striped != '':
                data += [l_striped]
        data = ' '.join(data)
    # Python
    elif ext.lower() in ['py', 'sh']:
        if type(path) == BytesIO:
            file = path.read().decode()
        else:
            with open(path) as f:
                file = f.read()
        # Parse the data
        for line in file.split('\n'):
            l_striped = line.strip()  # Removing trailing spaces
            if parse:
                l_striped = l_striped.format(**kwargs)  # Formating
            if remove_comments:
                l_striped = l_striped.split('#')[0]  # Remove comments
            if l_striped != '':
                data += [l_striped]
        data = ' '.join(data)
    # JavaScript
    elif ext.lower() in ['js']:
        if type(path) == BytesIO:
            file = path.read().decode()
        else:
            with open(path) as f:
                file = f.read()
        for line in file.split('\n'):  # Parse the data
            l_striped = line.strip()  # Removing trailing spaces
            if parse:
                l_striped = l_striped.format(**kwargs)  # Formating
            if remove_comments:
                l_striped = l_striped.split('//')[0]  # Remove comments
                re.sub(r"<!--(.|\s|\n)*?-->", "", l_striped.replace('/*', '<!--').replace('*/', '-->'))
            if l_striped != '':
                data += [l_striped]
        data = ' '.join(data)
    # Json
    elif ext.lower() in ['json']:
        if engine.lower() in ['json']:
            with open(path) as json_file:
                data = json.load(json_file)
        else:
            data = pd.read_json(path, **kwargs)
    elif ext.lower() in ['jsonc']:
        if type(path) == BytesIO:
            file = path.read().decode()
        else:
            with open(path) as f:
                file = f.read()
        for line in file.split('\n'):  # Parse the data
            l_striped = line.strip()
            if remove_comments:
                l_striped = re.sub(r"/\*(.|\s|\n)*?\*/", "", l_striped)
                l_striped = l_striped.split('//')[0]
                l_striped = l_striped.split('#')[0]
            if l_striped != '':
                data += [l_striped]
        str_content = ' '.join(data)
        # Ensure there is no comma at the end of the dict
        str_content = str_content.replace(", }", "}")
        data = json.loads(str_content)
    # Parquet
    elif ext.lower() in ['parq', 'parquet']:
        _engine = 'pyarrow' if engine == 'auto' else engine

        if orgn == 'S3':
            data = pd.read_parquet(path)
        elif type(_engine) == str:
            if _engine.lower() in ['py', 'pa', 'pyarrow']:
                import pyarrow.parquet as pq
                dataset = pq.ParquetDataset(path, **kwargs)
                table = dataset.read()
                data = table.to_pandas()
            elif _engine.lower() in ['fp', 'fastparquet']:
                from fastparquet import ParquetFile
                dataset = ParquetFile(path, **kwargs)
                table = dataset.to_pandas()
            else:
                raise ValueError('Engine value not allowed')
        else:
            data = _engine(path, **kwargs)
    # Feather
    elif ext.lower() in ['fea', 'feather']:
        from pyarrow.feather import read_table
        table = read_table(path, **kwargs)
        data = table.to_pandas()
    # Else, read-only
    elif ext.lower() in ['readonly', 'read-only', 'ro']:
        if type(path) == BytesIO:
            print(path.read().decode())
        else:
            with open(path) as f:
                for line in f:
                    print(line.rstrip())
    else:
        with open(path) as f:
            file = f.read()
        data = file
    # If not read-only
    return data

def f_read(*args, **kwargs):
    """Old function to load data file. This function is on deprecation path. Consider using :py:meth:`pycof.data.read` instead.

    .. warning::

        Note that from version 1.6.0, the `f_read` will be fully deprecated and replaced by the current :py:meth:`pycof.data.read`.


    :return: Output from :py:meth:`pycof.data.read`
    :rtype: :obj:`pandas.DataFrame`
    """
    warn('The function f_read will soon be deprecated. Consider using the function `read` instead.', DeprecationWarning, stacklevel=2)
    return read(*args, **kwargs)
