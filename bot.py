#!/usr/bin/env python3
"""
Team Communication Bot
Telegram bot for team coordination with Twitter API (multi-token rotation), Medium, 
Telegram channel integrations, and Influencer monitoring with batch requests.
"""

import os
import logging
import asyncio
import json
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
from collections import defaultdict

import feedparser
import httpx
import tweepy
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration from environment variables
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
SOURCE_CHANNEL_ID = os.getenv('SOURCE_CHANNEL_ID', '-1003415917767')
GOOGLE_SHEETS_CREDENTIALS = os.getenv('GOOGLE_CREDENTIALS', '')
GOOGLE_SHEET_ID = os.getenv('GOOGLE_SHEETS_ID', '1X3wRuWnE8Oh6Vz7DRruS7e4GuvpOHMnXuJ1M2R-TCcc')

# Multiple Twitter Bearer Tokens for rotation (to maximize API limits)
TWITTER_BEARER_TOKENS = []
for i in range(1, 10):  # Support up to 9 tokens
    token = os.getenv(f'TWITTER_BEARER_TOKEN_{i}', '')
    if token:
        TWITTER_BEARER_TOKENS.append(token)

# Fallback to single token if multi-token not configured
if not TWITTER_BEARER_TOKENS:
    single_token = os.getenv('TWITTER_BEARER_TOKEN', '')
    if single_token:
        TWITTER_BEARER_TOKENS.append(single_token)

# Twitter account to monitor (official)
TWITTER_ACCOUNT = 'beeos_arenavs'

# Medium account to monitor
MEDIUM_USERNAME = 'VS1.finance'

# ============================================
# TEAM MEMBERS CONFIGURATION
# ============================================

# Influencer managers - receive notifications about influencer posts
INFLUENCER_MANAGERS = {
    'Igor': 422264082,
    'Roman': 883728320,
    'Dyma': 6993427932
}

# Regular team members - receive Twitter, Medium, channel notifications
TEAM_MEMBERS = {
    'Mika': 449742082,
    'Marie': 276787341,
    'Phillipe': 1271922543,
    'Tsotne': 6733503938,
    'Fehmi': 5233426523,
    'Danyl': 1078976724
}

# All members combined (for backward compatibility)
ALL_MEMBERS = {**TEAM_MEMBERS, **INFLUENCER_MANAGERS}

# ============================================
# STATE MANAGEMENT
# ============================================

STATE_FILE = 'bot_state.json'


class BotState:
    """Manages persistent state for the bot."""
    
    def __init__(self):
        self.last_tweet_id: Optional[str] = None
        self.last_medium_article: Optional[str] = None
        self.influencer_last_tweets: Dict[str, str] = {}  # username -> last_tweet_id
        self.influencer_user_ids: Dict[str, str] = {}  # username -> twitter_user_id (cached)
        self.current_token_index: int = 0
        self.token_usage_counts: Dict[int, int] = defaultdict(int)
        self.manager_assignment_counts: Dict[str, int] = defaultdict(int)
        self.load()
    
    def load(self):
        """Load state from file."""
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r') as f:
                    data = json.load(f)
                    self.last_tweet_id = data.get('last_tweet_id')
                    self.last_medium_article = data.get('last_medium_article')
                    self.influencer_last_tweets = data.get('influencer_last_tweets', {})
                    self.influencer_user_ids = data.get('influencer_user_ids', {})
                    self.current_token_index = data.get('current_token_index', 0)
                    self.token_usage_counts = defaultdict(int, {int(k): v for k, v in data.get('token_usage_counts', {}).items()})
                    self.manager_assignment_counts = defaultdict(int, data.get('manager_assignment_counts', {}))
        except Exception as e:
            logger.error(f"Error loading state: {e}")
    
    def save(self):
        """Save state to file."""
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump({
                    'last_tweet_id': self.last_tweet_id,
                    'last_medium_article': self.last_medium_article,
                    'influencer_last_tweets': self.influencer_last_tweets,
                    'influencer_user_ids': self.influencer_user_ids,
                    'current_token_index': self.current_token_index,
                    'token_usage_counts': dict(self.token_usage_counts),
                    'manager_assignment_counts': dict(self.manager_assignment_counts)
                }, f)
        except Exception as e:
            logger.error(f"Error saving state: {e}")


# ============================================
# TWITTER API CLIENT WITH ROTATION
# ============================================

class MultiTokenTwitterClient:
    """Twitter API client with multiple token rotation for maximizing API limits."""
    
    def __init__(self, state: BotState):
        self.state = state
        self.clients: List[tweepy.Client] = []
        self._init_clients()
    
    def _init_clients(self):
        """Initialize Twitter API clients for all tokens."""
        for i, token in enumerate(TWITTER_BEARER_TOKENS):
            try:
                client = tweepy.Client(
                    bearer_token=token,
                    wait_on_rate_limit=False
                )
                self.clients.append(client)
                logger.info(f"Twitter API client {i+1} initialized")
            except Exception as e:
                logger.error(f"Error initializing Twitter client {i+1}: {e}")
        
        if self.clients:
            logger.info(f"Total Twitter clients initialized: {len(self.clients)}")
        else:
            logger.warning("No Twitter API clients initialized")
    
    def _try_all_clients(self, operation):
        """Try operation with all clients until one succeeds."""
        if not self.clients:
            return None
        
        errors = []
        start_index = self.state.current_token_index
        
        for i in range(len(self.clients)):
            client_index = (start_index + i) % len(self.clients)
            client = self.clients[client_index]
            
            try:
                result = operation(client)
                self.state.current_token_index = client_index
                self.state.token_usage_counts[client_index] += 1
                self.state.save()
                logger.info(f"API call successful with token {client_index + 1}")
                return result
            except tweepy.TooManyRequests as e:
                logger.warning(f"Rate limit on token {client_index + 1}, trying next...")
                errors.append(f"Token {client_index + 1}: Rate limited")
                continue
            except Exception as e:
                logger.error(f"Error with token {client_index + 1}: {e}")
                errors.append(f"Token {client_index + 1}: {str(e)}")
                continue
        
        logger.error(f"All tokens failed: {errors}")
        return None
    
    async def get_user_tweets(self, username: str, since_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch recent tweets from a user."""
        def operation(client):
            # Check if we have cached user_id
            user_id = self.state.influencer_user_ids.get(username)
            
            if not user_id:
                user = client.get_user(username=username)
                if not user or not user.data:
                    raise Exception(f"User not found: {username}")
                user_id = str(user.data.id)
                self.state.influencer_user_ids[username] = user_id
                self.state.save()
            
            kwargs = {
                'id': user_id,
                'max_results': 5,
                'tweet_fields': ['created_at', 'text', 'entities']
            }
            
            if since_id:
                kwargs['since_id'] = since_id
            
            return client.get_users_tweets(**kwargs)
        
        tweets = self._try_all_clients(operation)
        
        if not tweets or not tweets.data:
            return []
        
        result = []
        for tweet in tweets.data:
            tweet_url = f"https://x.com/{username}/status/{tweet.id}"
            result.append({
                'id': str(tweet.id),
                'text': tweet.text,
                'link': tweet_url,
                'username': username,
                'created_at': tweet.created_at.isoformat() if tweet.created_at else None
            })
        
        return result
    
    async def get_users_by_usernames_batch(self, usernames: List[str]) -> Dict[str, str]:
        """Get user IDs for multiple usernames in a single batch request (up to 100)."""
        if not usernames:
            return {}
        
        def operation(client):
            # Twitter API allows up to 100 usernames per request
            return client.get_users(usernames=usernames[:100], user_fields=['id'])
        
        result = self._try_all_clients(operation)
        
        if not result or not result.data:
            return {}
        
        user_ids = {}
        for user in result.data:
            user_ids[user.username.lower()] = str(user.id)
        
        return user_ids
    
    async def get_users_tweets_batch(self, user_ids: List[str]) -> Dict[str, List[Dict]]:
        """
        Get recent tweets for multiple users.
        Note: Twitter API doesn't support batch tweet fetching by multiple users,
        so we need to make individual requests but we optimize by caching user_ids.
        """
        all_tweets = {}
        
        for user_id in user_ids:
            def operation(client):
                return client.get_users_tweets(
                    id=user_id,
                    max_results=5,
                    tweet_fields=['created_at', 'text', 'author_id']
                )
            
            tweets = self._try_all_clients(operation)
            
            if tweets and tweets.data:
                all_tweets[user_id] = tweets.data
        
        return all_tweets


# ============================================
# TWITTER MONITOR (Official Account)
# ============================================

class TwitterMonitor:
    """Monitors official Twitter account for new posts."""
    
    def __init__(self, state: BotState, client: MultiTokenTwitterClient):
        self.state = state
        self.client = client
    
    async def check_for_new_tweets(self) -> List[Dict[str, Any]]:
        """Check for new tweets from official account."""
        tweets = await self.client.get_user_tweets(
            TWITTER_ACCOUNT, 
            since_id=self.state.last_tweet_id
        )
        
        if tweets:
            self.state.last_tweet_id = tweets[0]['id']
            self.state.save()
        
        return tweets
    
    def is_twitter_space(self, tweet: Dict[str, Any]) -> bool:
        """Check if tweet is about a Twitter Space."""
        text = tweet.get('text', '').lower()
        return 'space' in text or 'spaces' in text or 'twitter.com/i/spaces' in text


# ============================================
# INFLUENCER MONITOR (Batch Requests)
# ============================================

class InfluencerMonitor:
    """Monitors influencers for new posts using batch requests."""
    
    def __init__(self, state: BotState, client: MultiTokenTwitterClient):
        self.state = state
        self.client = client
        self.influencers: List[str] = []  # List of usernames
    
    async def load_influencers_from_sheets(self):
        """Load influencer list from Google Sheets."""
        if not GOOGLE_SHEETS_CREDENTIALS:
            logger.warning("Google Sheets credentials not configured")
            return
        
        try:
            import gspread
            from google.oauth2.service_account import Credentials
            
            creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS)
            creds = Credentials.from_service_account_info(
                creds_dict,
                scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
            )
            
            gc = gspread.authorize(creds)
            sheet = gc.open_by_key(GOOGLE_SHEET_ID)
            worksheet = sheet.get_worksheet(0)
            
            values = worksheet.col_values(1)
            
            self.influencers = []
            for value in values[1:]:  # Skip header
                if value:
                    username = self._extract_username(value)
                    if username:
                        self.influencers.append(username.lower())
            
            logger.info(f"Loaded {len(self.influencers)} influencers from Google Sheets")
            
            # Pre-cache user IDs for new influencers
            await self._cache_user_ids()
            
        except Exception as e:
            logger.error(f"Error loading influencers from Google Sheets: {e}")
    
    def _extract_username(self, value: str) -> Optional[str]:
        """Extract Twitter username from various formats."""
        value = value.strip()
        
        if value.startswith('@'):
            return value[1:]
        
        patterns = [
            r'(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/([a-zA-Z0-9_]+)',
            r'^([a-zA-Z0-9_]+)$'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, value)
            if match:
                username = match.group(1)
                if username.lower() not in ['i', 'intent', 'share', 'search', 'hashtag']:
                    return username
        
        return None
    
    async def _cache_user_ids(self):
        """Cache user IDs for influencers using batch requests."""
        # Find influencers without cached user_id
        uncached = [u for u in self.influencers if u.lower() not in self.state.influencer_user_ids]
        
        if not uncached:
            logger.info("All influencer user IDs already cached")
            return
        
        logger.info(f"Caching user IDs for {len(uncached)} new influencers...")
        
        # Process in batches of 100
        for i in range(0, len(uncached), 100):
            batch = uncached[i:i+100]
            user_ids = await self.client.get_users_by_usernames_batch(batch)
            
            for username, user_id in user_ids.items():
                self.state.influencer_user_ids[username.lower()] = user_id
            
            self.state.save()
            logger.info(f"Cached {len(user_ids)} user IDs (batch {i//100 + 1})")
            
            # Small delay between batches
            await asyncio.sleep(1)
    
    async def check_influencer_posts(self) -> List[Dict[str, Any]]:
        """
        Check for new posts from influencers.
        Uses cached user_ids to minimize API calls.
        Returns list of new posts with influencer info.
        """
        new_posts = []
        
        # Process influencers in batches to spread API usage
        batch_size = 50  # Check 50 influencers per run
        
        # Get influencers to check this run (rotate through list)
        total = len(self.influencers)
        if total == 0:
            return []
        
        # Simple rotation - check different subset each time
        check_list = self.influencers[:batch_size]
        
        logger.info(f"Checking {len(check_list)} influencers for new posts...")
        
        for username in check_list:
            username_lower = username.lower()
            
            # Get cached user_id
            user_id = self.state.influencer_user_ids.get(username_lower)
            if not user_id:
                continue
            
            try:
                # Get last known tweet_id for this influencer
                since_id = self.state.influencer_last_tweets.get(username_lower)
                
                tweets = await self.client.get_user_tweets(username, since_id=since_id)
                
                if tweets:
                    # Update last tweet id
                    self.state.influencer_last_tweets[username_lower] = tweets[0]['id']
                    
                    # Add to new posts
                    for tweet in tweets:
                        # Check if tweet mentions our target account
                        if f"@{TWITTER_ACCOUNT}".lower() in tweet['text'].lower():
                            new_posts.append(tweet)
                
                # Small delay between requests
                await asyncio.sleep(0.5)
                
            except Exception as e:
                logger.error(f"Error checking influencer {username}: {e}")
                continue
        
        if new_posts:
            self.state.save()
            logger.info(f"Found {len(new_posts)} new influencer posts mentioning @{TWITTER_ACCOUNT}")
        
        return new_posts


# ============================================
# MEDIUM MONITOR
# ============================================

class MediumMonitor:
    """Monitors Medium account for new articles."""
    
    def __init__(self, state: BotState):
        self.state = state
        self.feed_url = f"https://medium.com/feed/@{MEDIUM_USERNAME}"
    
    async def check_for_new_articles(self) -> List[Dict[str, Any]]:
        """Check for new Medium articles."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(self.feed_url, timeout=30)
                
                if response.status_code != 200:
                    logger.error(f"Medium feed error: {response.status_code}")
                    return []
                
                feed = feedparser.parse(response.text)
                
                if not feed.entries:
                    return []
                
                new_articles = []
                for entry in feed.entries[:5]:
                    article_id = entry.get('id', entry.get('link', ''))
                    
                    if self.state.last_medium_article and article_id == self.state.last_medium_article:
                        break
                    
                    new_articles.append({
                        'title': entry.get('title', 'Untitled'),
                        'link': entry.get('link', ''),
                        'summary': entry.get('summary', '')[:200] + '...' if entry.get('summary') else '',
                        'published': entry.get('published', '')
                    })
                
                if new_articles and feed.entries:
                    self.state.last_medium_article = feed.entries[0].get('id', feed.entries[0].get('link', ''))
                    self.state.save()
                
                return new_articles
                
        except Exception as e:
            logger.error(f"Error checking Medium: {e}")
            return []


# ============================================
# MANAGER ASSIGNER
# ============================================

class ManagerAssigner:
    """Assigns influencer notifications to managers based on fairness."""
    
    def __init__(self, state: BotState):
        self.state = state
    
    def assign_manager(self) -> tuple[str, int]:
        """Assign notification to a manager based on fairness (round-robin)."""
        managers = list(INFLUENCER_MANAGERS.items())
        
        # Find manager with least assignments
        min_count = float('inf')
        selected = managers[0]
        
        for name, telegram_id in managers:
            count = self.state.manager_assignment_counts.get(name, 0)
            if count < min_count:
                min_count = count
                selected = (name, telegram_id)
        
        # Update count
        self.state.manager_assignment_counts[selected[0]] += 1
        self.state.save()
        
        return selected


# ============================================
# COMMAND HANDLERS
# ============================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    is_member = chat_id in ALL_MEMBERS.values()
    is_manager = chat_id in INFLUENCER_MANAGERS.values()
    
    if is_member:
        role = "Influencer Manager" if is_manager else "Team Member"
        message = f"""Hey {user.first_name}!

You're in the VS1/BeeOS team list.
Role: {role}

You'll receive notifications about:
{"• New influencer posts mentioning @beeos_arenavs" if is_manager else "• New posts on X (@beeos_arenavs)"}
{"" if is_manager else "• Live X Spaces"}
{"" if is_manager else "• New Medium articles"}
{"" if is_manager else "• Team channel messages"}

Commands:
• /help - Command list
• /myid - Show my Telegram ID
• /stats - Show statistics"""
    else:
        message = f"""Hey {user.first_name}!

I'm the VS1/BeeOS team coordination bot.

Your Telegram ID is not in the team list.
ID: {chat_id}

If you're a team member, ask an admin to add your ID."""
    
    await update.message.reply_text(message)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    message = """Commands:
/start - Start
/help - Help
/myid - Show my Telegram ID
/stats - Show statistics

Notifications are sent based on your role in the team."""
    
    await update.message.reply_text(message)


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /myid command."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    is_member = chat_id in ALL_MEMBERS.values()
    is_manager = chat_id in INFLUENCER_MANAGERS.values()
    
    if is_manager:
        status = "Influencer Manager"
    elif is_member:
        status = "Team Member"
    else:
        status = "Not in team list"
    
    message = f"""Name: {user.first_name}
Telegram ID: {chat_id}
Status: {status}"""
    
    await update.message.reply_text(message)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats command."""
    chat_id = update.effective_chat.id
    
    # Only managers can see stats
    if chat_id not in INFLUENCER_MANAGERS.values():
        await update.message.reply_text("This command is only available for managers.")
        return
    
    state = context.bot_data.get('state')
    if not state:
        await update.message.reply_text("Stats not available.")
        return
    
    influencer_monitor = context.bot_data.get('influencer_monitor')
    
    stats_lines = ["📊 Bot Statistics\n"]
    
    # Influencer stats
    if influencer_monitor:
        stats_lines.append(f"Tracked influencers: {len(influencer_monitor.influencers)}")
        stats_lines.append(f"Cached user IDs: {len(state.influencer_user_ids)}")
    
    # Manager assignments
    stats_lines.append("\nManager assignments:")
    for manager in INFLUENCER_MANAGERS.keys():
        count = state.manager_assignment_counts.get(manager, 0)
        stats_lines.append(f"  {manager}: {count}")
    
    # Token usage
    if state.token_usage_counts:
        stats_lines.append(f"\nTwitter API token usage:")
        for token_idx, count in state.token_usage_counts.items():
            stats_lines.append(f"  Token {token_idx + 1}: {count} requests")
    
    await update.message.reply_text("\n".join(stats_lines))


# ============================================
# NOTIFICATION SENDERS
# ============================================

async def send_twitter_notification(bot: Bot, tweet: Dict[str, Any], is_space: bool = False):
    """Send Twitter notification to regular team members (not managers)."""
    username = tweet.get('username', TWITTER_ACCOUNT)
    
    if is_space:
        message = f"""🔴 X Space is LIVE!

@{username} started a Space:

{tweet.get('text', '')}

Join: {tweet.get('link', '')}

Please join and support the team!"""
    else:
        message = f"""📢 New post from @{username}

{tweet.get('text', '')}

Link: {tweet.get('link', '')}

Please like and repost to support!"""
    
    # Send only to regular team members (not managers)
    for name, telegram_id in TEAM_MEMBERS.items():
        try:
            await bot.send_message(chat_id=telegram_id, text=message)
        except Exception as e:
            logger.error(f"Error sending Twitter notification to {name}: {e}")


async def send_medium_notification(bot: Bot, article: Dict[str, Any]):
    """Send Medium article notification to regular team members."""
    message = f"""📝 New Medium article

Title: {article.get('title', 'Untitled')}

{article.get('summary', '')}

Read: {article.get('link', '')}

Please read and clap to support!"""
    
    # Send only to regular team members
    for name, telegram_id in TEAM_MEMBERS.items():
        try:
            await bot.send_message(chat_id=telegram_id, text=message)
        except Exception as e:
            logger.error(f"Error sending Medium notification to {name}: {e}")


async def send_influencer_notification(bot: Bot, post: Dict[str, Any], manager_name: str, manager_id: int):
    """Send influencer post notification to assigned manager."""
    message = f"""🎯 Influencer post from @{post.get('username', 'unknown')}

{post.get('text', '')}

Link: {post.get('link', '')}

Please engage: like, repost, comment from official account."""
    
    try:
        await bot.send_message(chat_id=manager_id, text=message)
        logger.info(f"Influencer notification sent to {manager_name}")
    except Exception as e:
        logger.error(f"Error sending influencer notification to {manager_name}: {e}")


async def send_channel_notification(bot: Bot, message_text: str):
    """Send channel message notification to regular team members."""
    notification = f"""📣 New message from team channel

{message_text}

Please check and engage if needed."""
    
    # Send only to regular team members
    for name, telegram_id in TEAM_MEMBERS.items():
        try:
            await bot.send_message(chat_id=telegram_id, text=notification)
        except Exception as e:
            logger.error(f"Error sending channel notification to {name}: {e}")


# ============================================
# SCHEDULED JOBS
# ============================================

async def check_twitter_job(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled job to check Twitter for new posts from official account."""
    twitter_monitor = context.bot_data.get('twitter_monitor')
    if not twitter_monitor:
        return
    
    try:
        logger.info("Checking official Twitter account...")
        tweets = await twitter_monitor.check_for_new_tweets()
        
        for tweet in tweets:
            is_space = twitter_monitor.is_twitter_space(tweet)
            await send_twitter_notification(context.bot, tweet, is_space)
            
    except Exception as e:
        logger.error(f"Error in Twitter check job: {e}")


async def check_medium_job(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled job to check Medium for new articles."""
    medium_monitor = context.bot_data.get('medium_monitor')
    if not medium_monitor:
        return
    
    try:
        logger.info("Checking Medium...")
        articles = await medium_monitor.check_for_new_articles()
        
        for article in articles:
            await send_medium_notification(context.bot, article)
            
    except Exception as e:
        logger.error(f"Error in Medium check job: {e}")


async def check_influencers_job(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled job to check influencer posts (every 6 hours)."""
    influencer_monitor = context.bot_data.get('influencer_monitor')
    manager_assigner = context.bot_data.get('manager_assigner')
    
    if not influencer_monitor or not manager_assigner:
        return
    
    try:
        logger.info("Checking influencer posts...")
        posts = await influencer_monitor.check_influencer_posts()
        
        for post in posts:
            manager_name, manager_id = manager_assigner.assign_manager()
            await send_influencer_notification(context.bot, post, manager_name, manager_id)
            
    except Exception as e:
        logger.error(f"Error in influencer check job: {e}")


async def reload_influencers_job(context: ContextTypes.DEFAULT_TYPE):
    """Periodically reload influencer list from Google Sheets."""
    influencer_monitor = context.bot_data.get('influencer_monitor')
    if influencer_monitor:
        logger.info("Reloading influencer list...")
        await influencer_monitor.load_influencers_from_sheets()


# ============================================
# CHANNEL POST HANDLER
# ============================================

async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle new posts from the source channel."""
    if not update.channel_post:
        return
    
    channel_id = str(update.channel_post.chat.id)
    
    if channel_id != SOURCE_CHANNEL_ID and channel_id != SOURCE_CHANNEL_ID.replace('-100', ''):
        return
    
    message_text = update.channel_post.text or update.channel_post.caption or ""
    
    if not message_text:
        return
    
    await send_channel_notification(context.bot, message_text)


# ============================================
# MAIN FUNCTION
# ============================================

def main():
    """Main function to run the bot."""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        return
    
    # Initialize state and monitors
    state = BotState()
    twitter_client = MultiTokenTwitterClient(state)
    twitter_monitor = TwitterMonitor(state, twitter_client)
    medium_monitor = MediumMonitor(state)
    influencer_monitor = InfluencerMonitor(state, twitter_client)
    manager_assigner = ManagerAssigner(state)
    
    # Build application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Store monitors in bot_data
    application.bot_data['state'] = state
    application.bot_data['twitter_monitor'] = twitter_monitor
    application.bot_data['medium_monitor'] = medium_monitor
    application.bot_data['influencer_monitor'] = influencer_monitor
    application.bot_data['manager_assigner'] = manager_assigner
    
    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("myid", myid_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post))
    
    # Setup scheduler
    scheduler = AsyncIOScheduler()
    
    # Twitter official account check every 3 hours (SAFE MODE)
    scheduler.add_job(
        check_twitter_job,
        'interval',
        hours=3,
        args=[application],
        id='twitter_check'
    )
    
    # Medium check every 30 minutes
    scheduler.add_job(
        check_medium_job,
        'interval',
        minutes=30,
        args=[application],
        id='medium_check'
    )
    
    # Influencer check every 6 hours (SAFE: uses batch + cached user_ids)
    # ~4 checks/day * 3-4 API calls = ~12-16 requests/day = ~360-480/month
    # With 4 tokens = 400 requests/month, this is within limits
    scheduler.add_job(
        check_influencers_job,
        'interval',
        hours=6,
        args=[application],
        id='influencer_check'
    )
    
    # Reload influencers from Google Sheets every 12 hours
    scheduler.add_job(
        reload_influencers_job,
        'interval',
        hours=12,
        args=[application],
        id='reload_influencers'
    )
    
    scheduler.start()
    
    # Log startup info
    logger.info("=" * 50)
    logger.info("VS1/BeeOS Team Bot Starting")
    logger.info("=" * 50)
    logger.info(f"Twitter tokens available: {len(TWITTER_BEARER_TOKENS)}")
    logger.info(f"Regular team members: {len(TEAM_MEMBERS)} ({', '.join(TEAM_MEMBERS.keys())})")
    logger.info(f"Influencer managers: {len(INFLUENCER_MANAGERS)} ({', '.join(INFLUENCER_MANAGERS.keys())})")
    logger.info(f"Source channel ID: {SOURCE_CHANNEL_ID}")
    logger.info("Schedule:")
    logger.info("  - Twitter @beeos_arenavs: every 3 hours")
    logger.info("  - Medium: every 30 minutes")
    logger.info("  - Influencer posts: every 6 hours")
    logger.info("  - Reload influencer list: every 12 hours")
    logger.info(f"API Safety: ~360-480 requests/month, Limit: {len(TWITTER_BEARER_TOKENS) * 100}/month")
    logger.info("=" * 50)
    
    # Load influencers on startup
    async def startup():
        await influencer_monitor.load_influencers_from_sheets()
    
    asyncio.get_event_loop().run_until_complete(startup())
    
    logger.info("Bot started!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
