#!/usr/bin/env python3
"""
推特自动发帖脚本
依赖: tweepy (pip install tweepy)
配置: twitter_config.json
用法: python3 twitter_post.py --text "内容"
      python3 twitter_post.py --sync-square  # 读取待发帖队列并同步
"""

import json
import os
import sys
import argparse
import tweepy
from datetime import datetime

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'twitter_config.json')
SQUARE_CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'square_config.json')


def get_client():
    with open(CONFIG_PATH) as f:
        tc = json.load(f)
    client = tweepy.Client(
        consumer_key=tc['consumer_key'],
        consumer_secret=tc['consumer_secret'],
        access_token=tc['access_token'],
        access_token_secret=tc['access_token_secret']
    )
    return client


def post_tweet(text: str, dry_run: bool = False):
    """发一条推文，返回 tweet id"""
    if dry_run:
        print(f"[DRY RUN] 推文内容 ({len(text)} chars):\n{text}\n")
        return None
    client = get_client()
    response = client.create_tweet(text=text)
    tweet_id = response.data['id']
    print(f"✅ 发推成功: https://x.com/i/web/status/{tweet_id}")
    return tweet_id


def verify_auth():
    """验证推特授权是否有效"""
    client = get_client()
    me = client.get_me()
    print(f"✅ 授权有效，账户: @{me.data.username} ({me.data.name})")
    return me.data


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='推特自动发帖')
    parser.add_argument('--text', type=str, help='推文内容')
    parser.add_argument('--verify', action='store_true', help='验证授权')
    parser.add_argument('--dry-run', action='store_true', help='预览模式不真正发帖')
    args = parser.parse_args()

    if args.verify:
        verify_auth()
    elif args.text:
        post_tweet(args.text, dry_run=args.dry_run)
    else:
        parser.print_help()
