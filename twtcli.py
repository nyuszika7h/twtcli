#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import sys
import time
import uuid

import requests
import toml

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'config.toml')

config = toml.load(CONFIG_PATH)

parser = argparse.ArgumentParser()
parser.add_argument(
    '-2',
    '--api-v2',
    action='store_true',
    help='use API v2 instead of v1.1',
)
parser.add_argument(
    '-p',
    '--post',
    action='store_true',
    help='send a POST request instead of GET',
)
parser.add_argument(
    '-j',
    '--json',
    action='store_true',
    help='send POST data as JSON',
)
parser.add_argument(
    '-d',
    '--data',
    action='append',
    default=[],
    help='query parameters or POST data',
)
parser.add_argument(
    '-w',
    '--wait',
    type=float,
    default=0,
    help='override wait time between requests',
)
parser.add_argument(
    '-F',
    '--ignore-ratelimit',
    action='store_true',
    help="don't add delays to accommodate rate limits",
)
parser.add_argument(
    '-c',
    '--follow-cursor',
    action='store_true',
    help='automatically follow next_cursor to fetch multiple pages',
)
parser.add_argument(
    '-N',
    '--no-resume',
    action='store_true',
    help='do not automatically save/restore last cursor',
)
parser.add_argument(
    '-D',
    '--debug',
    action='store_true',
    help='enable debug logging',
)
parser.add_argument(
    'user',
    help='user to authenticate as',
)
parser.add_argument(
    'endpoint',
    help='API endpoint',
)
args = parser.parse_args()


def log(s):
    print(f'[{time.strftime("%H:%M:%S")}]', s, file=sys.stderr)


if args.debug:
    debug = log
else:
    debug = lambda _: None

api_version = '2' if args.api_v2 else '1.1'
endpoint = args.endpoint
if endpoint.endswith('.json'):
    endpoint = endpoint[:-5]
url = f'https://api.twitter.com/{api_version}/{endpoint}.json'

app_token = config['app']['token']
csrf_token = uuid.uuid4().hex
auth_token = config['users'][args.user]

session = requests.Session()

session.headers = {
    'accept': 'application/json',
    'authorization': f'Bearer {app_token}',
    'content-type': 'application/x-www-form-urlencoded',
    'cookie': f'auth_token={auth_token}; ct0={csrf_token}',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/72.0.3626.109 Safari/537.36',
    'x-csrf-token': csrf_token,
}

req_args = {'params': {}}
data = {}

for x in args.data:
    (k, v) = x.split('=', 1)
    data[k] = v

if args.post:
    method = 'POST'
    if args.json:
        req_args['json'] = data
    else:
        req_args['data'] = data
else:
    method = 'GET'
    req_args['params'] = data

if 'cursor' in req_args['params']:
    cursor = req_args['params']['cursor']
else:
    cursor = None

if 'max_id' in req_args['params']:
    max_id = req_args['params']['max_id']
else:
    max_id = None

req_data = {'method': method, 'url': url, **req_args}
req_data = json.dumps(req_data, sort_keys=True)
req_hash = hashlib.sha256(req_data.encode('utf-8')).hexdigest()
debug(f'Request hash: {req_hash}')

cursor_file = os.path.join('.cursor', req_hash)
os.makedirs(os.path.dirname(cursor_file), exist_ok=True)

with open(cursor_file, 'a+') as fd:
    fd.seek(0)

    if not (args.no_resume or cursor or max_id):
        try:
            data = json.load(fd)
            cursor = data.get('cursor')
            max_id = data.get('max_id')
        except (FileNotFoundError, ValueError):
            pass
        else:
            if cursor or max_id:
                log(f'Resuming from cursor {cursor or max_id} (use --no-resume to disable)')

    while True:
        req_args['params'].update({'cursor': cursor, 'max_id': max_id})

        fd.seek(0)
        fd.truncate()
        json.dump({'cursor': cursor, 'max_id': max_id}, fd)

        debug(f'{method} {url!r} {req_args!r}')

        r = session.request(
            method,
            url,
            **req_args,
        )

        # print(r.request.body)

        if r.status_code in (420, 429):
            reset = float(r.headers.get('x-rate-limit-reset') or 0)

            if reset:
                delay = reset - time.time()
                log(f'Rate limit exceeded, reset in {delay:.1f}s')
                time.sleep(delay)
                continue
            else:
                log('Rate limit exceeded, reset time unknown - exiting')
                sys.exit(1)

        if not r.ok:
            r.raise_for_status()

        if r.status_code == 204:
            resp = 'OK'
        else:
            resp = r.json()

        print(json.dumps(resp))

        try:
            cursor = resp.get('next_cursor')
        except AttributeError:
            pass

        try:
            max_id = resp[-1]['id']
        except (AttributeError, IndexError, KeyError, TypeError):
            pass

        if not args.follow_cursor:
            break

        if args.ignore_ratelimit:
            if args.wait:
                debug(f'Sleeping for {args.wait}s')
                time.sleep(args.wait)
        else:
            # rate_limit = r.headers.get('x-rate-limit-limit')
            # if rate_limit:
            #     delay = 900 / float(rate_limit)
            #     debug(f'Sleeping for {delay}s (ratelimit)')
            #     time.sleep(delay)

            rl_limit = r.headers.get('x-rate-limit-limit')
            rl_remain = r.headers.get('x-rate-limit-remaining')
            rl_reset = r.headers.get('x-rate-limit-reset')

            if rl_remain and rl_reset:
                delta = float(rl_reset) - time.time()
                delay = max(delta / float(rl_remain), 0)
                debug(f'Sleeping for {delay:.2f}s (ratelimit {rl_remain}/{rl_limit}, reset {delta:.0f}s)')
                time.sleep(delay)
            else:
                debug(f'Sleeping for {args.wait}s')
                time.sleep(args.wait)

        if not (args.follow_cursor and (cursor or max_id)):
            break

if not cursor:
    try:
        os.remove(cursor_file)
    except FileNotFoundError:
        pass
