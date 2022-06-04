#!/usr/bin/env python
# coding: utf-8

import os
import random
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import pymongo
import requests
import yt_dlp
from dotenv import load_dotenv
from internetarchive import get_item, upload
from loguru import logger
from tqdm import tqdm

from clean_name import clean_fname
from jsonbin_manager import JSONBin


class NoStorageSecretFound(Exception):
    pass


class TimeLimitReached(Exception):
    pass


def alarm_handler(signum, _):
    print('Signal handler called with signal', signum)
    raise TimeLimitReached('Request is taking too long. Skipping...')


def load_data():
    jsonbin = False
    mongodb = False

    if os.getenv('MONGODB_CONNECTION_STRING'):
        client = pymongo.MongoClient(os.getenv('MONGODB_CONNECTION_STRING'))
        db = client['yt']
        col = db['DATA']
        data = list(db['DATA'].find({}))
        mongodb = True
        bin_id = None

    elif os.getenv('JSONBIN_KEY'):
        jb = JSONBin(os.getenv('JSONBIN_KEY'))
        bin_id = jb.handle_collection_bins()
        data = jb.read_bin(bin_id)['record']
        jsonbin = True
        col = None

    else:
        raise NoStorageSecretFound('You need at least one storage secret ('
                                   '`MONGODB_CONNECTION_STRING` or '
                                   '`JSONBIN_KEY`!')
    random.shuffle(data)
    return mongodb, jsonbin, col, bin_id, data


def archive_yt_channel(skip_list: Optional[list] = None) -> None:

    mongodb, jsonbin, col, bin_id, data = load_data()

    for video in tqdm(data):

        _id = video['_id']
        if skip_list:
            if _id in skip_list:
                logger.debug(f'Skipped {video} (skip list)...')
                continue

        ts = video['upload_date']
        y, m, d = ts[:4], ts[4:6], ts[6:]
        clean_name = clean_fname(video["title"])
        title = f'{y}-{m}-{d}__{clean_name}'
        fname = f'{title}.mp4'

        ydl_opts = {
            'format': 'mp4/bestaudio+bestvideo',
            'outtmpl': fname,
            'quiet': True,
            'no-warnings': True,
            'no-progress': True
        }

        if video['downloaded'] and not video['uploaded']:
            if not Path(fname).exists():
                video['downloaded'] = False

        if not video['downloaded']:
            logger.debug(f'🚀 (CURRENT DOWNLOAD) -> File: {fname}; YT title: '
                         f'{video["title"]}; YT URL: {video["url"]}')

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download(video['url'])
            except yt_dlp.utils.DownloadError as e:
                logger.error(f'❌ Error with video: {video}')
                logger.error(f'❌ ERROR message: {e}')
                logger.debug('Trying again with no format specification...')

                try:
                    with yt_dlp.YoutubeDL({'outtmpl': fname}) as ydl:
                        ydl.download(video['url'])
                except yt_dlp.utils.DownloadError as e:
                    logger.error(f'❌ Failed again! ERROR message: {e}')
                    logger.error(f'❌ Skipping ({video["url"]})...')
                    continue

            if mongodb:
                col.update_one({'_id': _id}, {'$set': {'downloaded': True}})
            elif jsonbin:
                video['downloaded'] = True
                jb.update_bin(bin_id, data)

        publish_date = f'{y}-{m}-{d} 00:00:00'

        identifier = f'{y}-{m}-{d}_{video["channel_name"]}'
        left_len = 80 - (len(identifier) + 1)
        identifier = f'{identifier}_{clean_name[:left_len]}'

        custom_fields = {
            k: v
            for k, v in video.items()
            if k not in ['_id', 'downloaded', 'uploaded']
        }
        md = {
            'collection':
            'opensource_movies',
            'mediatype':
            'movies',
            'description':
            f'Title: {video["title"]}\nPublished on: {publish_date}\n'
            f'Original video URL: {video["url"]}',
            'subject':
            video['channel_name'],
            'id':
            _id,
            **custom_fields
        }

        if not video['uploaded']:
            logger.debug(f'Upload metadata: {md}')
            identifier = identifier.replace(' ', '').strip()
            cur_metadata = get_item(identifier).item_metadata
            if cur_metadata.get('metadata'):
                archive_email = os.getenv('ARCHIVE_USER_EMAIL')
                if cur_metadata['metadata']['uploader'] != archive_email:
                    identifier = str(uuid.uuid4())
                else:
                    logger.debug(f'{_id} is already uploaded...')
                    continue

            logger.debug(f'🚀 (CURRENT UPLOAD) -> File: {fname}; Identifier: '
                         f'{identifier}; YT title: {video["title"]}; YT URL: '
                         f'{video["url"]}')

            try:
                r = upload(identifier, files=[fname], metadata=md)
            except requests.exceptions.HTTPError as e:
                if 'Slow Down' in str(e) or 'reduce your request rate' in str(
                        e):
                    logger.error(f'❌ Error with video: {video}')
                    logger.error(f'❌ ERROR message: {e}')
                    logger.debug('Sleeping for 60 seconds...')
                    time.sleep(60)
                    logger.debug('Trying to upload again...')

                    try:
                        r = upload(identifier, files=[fname], metadata=md)
                    except requests.exceptions.HTTPError as e:
                        logger.error('❌ Failed again!')
                        logger.error(f'❌ ERROR message: {e}')

                        try:
                            identifier = str(uuid.uuid4())
                            r = upload(identifier, files=[fname], metadata=md)
                        except requests.exceptions.HTTPError as e:
                            logger.error(f'❌ ERROR message: {e}')
                            logger.error(
                                '❌ Failed all attempts to upload! Skipping...')
                            continue

            status_code = r[0].status_code  # noqa
            if status_code == 200:
                if mongodb:
                    col.update_one({'_id': _id}, {'$set': {'uploaded': True}})
                elif jsonbin:
                    video['uploaded'] = True
                    jb.update_bin(bin_id, data)
                Path(fname).unlink()
            else:
                logger.error(f'❌ Could not upload {video}!')
                logger.error(f'❌ Status code error with video: {video}')

        # Update database in current loop for running concurrent jobs
        mongodb, jsonbin, col, bin_id, data = load_data()
    return


if __name__ == '__main__':
    load_dotenv()
    if '--no-logs' in sys.argv:
        logger.remove()

    signal.alarm(int(5.5 * 3600))

    try:
        archive_yt_channel()
        signal.alarm(0)
    except TimeLimitReached:
        logger.debug('The GitHub action is about to die. Exiting...')
        exit(0)
