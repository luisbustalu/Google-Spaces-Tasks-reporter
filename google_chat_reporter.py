#!/usr/bin/python3
"""
Google Spaces Tasks Reporter

IMPORTANT LIMITATIONS:
=====================

This tool has a fundamental limitation: tasks created via Google Chat are NOT accessible 
through the Google Tasks API. This means:

✅ WHAT WE CAN TRACK (via Chat API):
- Task creation events from Chat messages
- Task assignment changes
- Task completion notifications
- Task deletion notifications
- Basic task metadata (assignee, creation time, space)

❌ WHAT WE CANNOT TRACK (via Tasks API):
- Task titles and descriptions (only generic "Created a task for @Person")
- Due dates
- Detailed task status from Tasks API
- Task notes and comments
- Task list organization

For detailed explanation, see: GOOGLE_CHAT_TASKS_LIMITATIONS.md

The tool now operates in "Chat-only mode" focusing on what's available through
the Google Chat API rather than attempting integration with Google Tasks API.
"""

import os
import logging
import json
import argparse
from datetime import datetime, timedelta
from typing import List, Dict, Tuple
import unicodedata
import time
import fnmatch
import csv
from collections import defaultdict

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.errors import HttpError

# Constants
# NOTE: Google Tasks API scope removed - see GOOGLE_CHAT_TASKS_LIMITATIONS.md
# Tasks created via Google Chat are NOT accessible through Google Tasks API
SCOPES = [
    'https://www.googleapis.com/auth/chat.spaces',
    'https://www.googleapis.com/auth/chat.messages',
    'https://www.googleapis.com/auth/chat.messages.readonly',
]
TOKEN_FILE = 'config/token.json'
CREDENTIALS_FILE = 'config/client_secret.json'

def setup_logging():
    """
    Setup logging configuration.
    
    Logging location priority:
    1. If LOG_DIR environment variable is set, use that directory
    2. If ../logs directory exists, use that
    3. Otherwise, log to console only (stderr)
    
    This allows flexible deployment:
    - Development: logs to console
    - CGI deployment under wwwroot: logs can go to ../logs (outside wwwroot)
    - Custom deployment: set LOG_DIR environment variable
    """
    # Suppress specific warnings from Google API client
    logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)
    logging.getLogger('googleapiclient.discovery').setLevel(logging.ERROR)
    
    # Determine log directory
    log_dir = None
    log_file = None
    
    # Check for environment variable first
    if os.environ.get('LOG_DIR'):
        log_dir = os.environ.get('LOG_DIR')
        if os.path.isdir(log_dir):
            log_file = os.path.join(log_dir, 'google_chat_reporter.log')
    
    # If no env var, check for ../logs directory
    if not log_file:
        parent_logs = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
        if os.path.isdir(parent_logs):
            log_dir = parent_logs
            log_file = os.path.join(parent_logs, 'google_chat_reporter.log')
    
    # Configure logging
    if log_file:
        # Log to file
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler()  # Also log to console
            ]
        )
    else:
        # Log to console only
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )

def get_credentials() -> Credentials:
    """Fetch or refresh Google API credentials."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=7276, access_type="offline", prompt='consent')
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())

    return creds

def retry_on_error(max_retries=3, delay=30):
    """
    Decorator that retries a function on failure with a delay.
    
    Args:
        max_retries (int): Maximum number of retry attempts
        delay (int): Delay in seconds between retries
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            retries = 0
            while retries < max_retries:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    retries += 1
                    if retries == max_retries:
                        logging.error(f"Failed after {max_retries} attempts: {str(e)}")
                        raise
                    logging.warning(f"Attempt {retries} failed: {str(e)}. Retrying in {delay} seconds...")
                    time.sleep(delay)
            return None
        return wrapper
    return decorator

@retry_on_error()
def get_spaces(service) -> List[Dict]:
    """Retrieve all spaces from Google Chat, excluding DIRECT_MESSAGE spaces.
    
    Respects IGNORE_SPACES environment variable for filtering.
    IGNORE_SPACES format: JSON array of space IDs without "spaces/" prefix
    Example: '["AAAAMj0BPws", "AAAAfPFB3gs"]'
    """
    spaces = []
    page_token = None
    while True:
        response = service.spaces().list(pageToken=page_token).execute()
        for space in response.get('spaces', []):
            if space.get('spaceType') != 'SPACE':  # Exclude DIRECT_MESSAGE spaces
                continue
            spaces.append(space)
        page_token = response.get('nextPageToken')
        if not page_token:
            break
    
    # Apply IGNORE_SPACES filtering
    ignore_spaces_env = os.environ.get('IGNORE_SPACES', '')
    if ignore_spaces_env:
        try:
            ignored_ids = json.loads(ignore_spaces_env)
            space_blacklist = [f"spaces/{space_id}" for space_id in ignored_ids]
            original_count = len(spaces)
            spaces = [s for s in spaces if s['name'] not in space_blacklist]
            logging.info(f"Filtered {original_count - len(spaces)} spaces via IGNORE_SPACES")
        except json.JSONDecodeError as e:
            logging.error(f"Error parsing IGNORE_SPACES: {e}")
    
    return spaces

def get_public_spaces(service) -> List[Dict]:
    """Retrieve only public spaces (SPACE type)."""
    return get_spaces(service)  # Reuse existing function

def get_direct_message_spaces(service) -> List[Dict]:
    """Retrieve only direct message spaces (DIRECT_MESSAGE type)."""
    spaces = []
    page_token = None
    while True:
        response = service.spaces().list(pageToken=page_token).execute()
        for space in response.get('spaces', []):
            if space.get('spaceType') == 'DIRECT_MESSAGE':  # Only include DIRECT_MESSAGE spaces
                spaces.append(space)
        page_token = response.get('nextPageToken')
        if not page_token:
            break
    return spaces

def get_all_spaces_and_dms(service) -> List[Dict]:
    """Retrieve all spaces including direct messages."""
    spaces = []
    page_token = None
    while True:
        response = service.spaces().list(pageToken=page_token).execute()
        spaces.extend(response.get('spaces', []))
        page_token = response.get('nextPageToken')
        if not page_token:
            break
    return spaces

def matches_assignee_pattern(assignee_name: str, pattern: str) -> bool:
    """
    Check if an assignee name matches a pattern.
    Supports glob patterns (e.g., '*riyanka D*') with standard Unix glob matching (case-sensitive).
    
    Args:
        assignee_name: The actual assignee name to check
        pattern: The search pattern (can contain wildcards * and ?)
        
    Returns:
        True if the name matches the pattern, False otherwise
    """
    # Use fnmatch for standard Unix glob pattern matching
    return fnmatch.fnmatch(assignee_name, pattern)

def save_to_json(data: List[Dict], filename: str):
    """Save data to a JSON file with UTF-8 encoding."""
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)  # Ensure non-ASCII characters are preserved
    logging.info(f"Data saved to {filename}")

def clean_text_for_csv(text: str) -> str:
    """Clean text for CSV export by removing line breaks and escaping quotes."""
    if not text:
        return ""
    
    # Remove line breaks and replace with spaces
    cleaned = text.replace('\n', ' ').replace('\r', ' ')
    
    # Replace multiple spaces with single space
    cleaned = ' '.join(cleaned.split())
    
    # Escape quotes by doubling them (CSV standard)
    cleaned = cleaned.replace('"', '""')
    
    return cleaned

def save_to_csv(data: List[Dict], filename: str):
    """Save data to a CSV file with UTF-8 encoding and headers."""
    if not data:
        logging.warning("No data to save to CSV")
        return
    
    # Clean text fields for CSV export
    cleaned_data = []
    for item in data:
        cleaned_item = item.copy()
        # Clean text fields that might contain line breaks or quotes
        text_fields = ['first_thread_message', 'message_text', 'text', 'task_description', 'task_name']
        for field in text_fields:
            if field in cleaned_item and cleaned_item[field]:
                cleaned_item[field] = clean_text_for_csv(str(cleaned_item[field]))
        cleaned_data.append(cleaned_item)
    
    # Get all unique keys from all dictionaries to ensure consistent columns
    all_keys = []
    seen = set()
    for item in cleaned_data:
        for key in item.keys():
            if key not in seen:
                all_keys.append(key)
                seen.add(key)
    
    # Write to CSV using built-in csv module
    with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=all_keys)
        writer.writeheader()
        writer.writerows(cleaned_data)
    
    logging.info(f"Data saved to {filename}")

def save_data(data: List[Dict], filename: str, output_format: str = "json"):
    """Save data in the specified format (json or csv)."""
    if output_format.lower() == "csv":
        save_to_csv(data, filename)
    else:  # Default to JSON
        save_to_json(data, filename)

def load_from_json(filename: str) -> List[Dict]:
    """Load data from a JSON file."""
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None

@retry_on_error()
def get_user_display_name(creds: Credentials, user_resource_name: str) -> str:
    """Fetch the display name of a user using the Google People API."""
    try:
        if user_resource_name.startswith('users/'):
            user_resource_name = user_resource_name.replace('users/', 'people/')

        people_service = build('people', 'v1', credentials=creds)
        profile = people_service.people().get(
            resourceName=user_resource_name,
            personFields='names'
        ).execute()

        if 'names' in profile:
            for name in profile['names']:
                if 'displayName' in name:
                    return name['displayName']
    except Exception as e:
        logging.error(f"Error fetching profile for user {user_resource_name}: {e}")
        raise
    return None


@retry_on_error()
def get_messages_for_space(service, space_name: str, date_start: str, date_end: str):
    """Helper function to get messages from a space with retry logic."""
    page_token = None
    messages = []
    while True:
        response = service.spaces().messages().list(
            parent=space_name,
            pageToken=page_token,
            filter=f'createTime > "{date_start}" AND createTime < "{date_end}"'
        ).execute()
        messages.extend(response.get('messages', []))
        page_token = response.get('nextPageToken')
        if not page_token:
            break
    return messages

@retry_on_error()
def get_first_thread_message(service, space_name: str, thread_name: str) -> Dict:
    """
    Retrieve just the first message from a thread for lightweight context.
    
    Args:
        service: Google Chat API service instance
        space_name: Name of the Chat space (e.g., 'spaces/AAAA')
        thread_name: Name of the thread (e.g., 'spaces/AAAA/threads/XyBuRxxncFM')
        
    Returns:
        Dictionary containing the first message, or empty dict if none found
    """
    try:
        # Get messages in the thread with a limit of 1 to get just the first message
        response = service.spaces().messages().list(
            parent=space_name,
            pageSize=1,  # Only get the first message
            filter=f'thread.name="{thread_name}"'
        ).execute()
        
        messages = response.get('messages', [])
        if messages:
            # Verify this message is from the correct thread
            message = messages[0]
            if message.get('thread', {}).get('name') == thread_name:
                return message
        
        return {}
        
    except Exception as e:
        logging.warning(f"Could not retrieve first message for thread {thread_name}: {e}")
        return {}

@retry_on_error()
def get_thread_messages(service, space_name: str, thread_name: str) -> List[Dict]:
    """
    Retrieve all messages from a specific thread.
    
    Args:
        service: Google Chat API service instance
        space_name: Name of the Chat space (e.g., 'spaces/AAAA')
        thread_name: Name of the thread (e.g., 'spaces/AAAA/threads/XyBuRxxncFM')
        
    Returns:
        List of message dictionaries ordered chronologically (oldest first)
    """
    try:
        # Get all messages in the thread
        page_token = None
        messages = []
        
        while True:
            # Use the thread name to filter messages in this specific thread
            response = service.spaces().messages().list(
                parent=space_name,
                pageToken=page_token,
                # Filter by thread name - this should get only messages from this thread
                filter=f'thread.name="{thread_name}"'
            ).execute()
            
            thread_messages = response.get('messages', [])
            # Filter to ensure we only get messages from the specific thread
            for message in thread_messages:
                if message.get('thread', {}).get('name') == thread_name:
                    messages.append(message)
            
            page_token = response.get('nextPageToken')
            if not page_token:
                break
        
        # Sort messages by creation time to ensure chronological order
        messages.sort(key=lambda x: x.get('createTime', ''))
        
        logging.debug(f"Retrieved {len(messages)} messages from thread {thread_name}")
        return messages
        
    except Exception as e:
        logging.warning(f"Could not retrieve thread messages for {thread_name}: {e}")
        # If thread-specific retrieval fails, return empty list
        # This maintains backward compatibility
        return []

def get_thread_info(service, space_name: str, thread_name: str) -> Dict:
    """
    Get information about a thread including first message and message count.
    
    Args:
        service: Google Chat API service instance
        space_name: Name of the Chat space
        thread_name: Name of the thread
        
    Returns:
        Dictionary containing thread information:
        - first_message: The first (oldest) message in the thread
        - message_count: Total number of messages in the thread
        - first_message_text: Text content of the first message
        - thread_starter: Display name of who started the thread
        - last_message_time: Creation time of the last message in the thread
    """
    thread_messages = get_thread_messages(service, space_name, thread_name)
    
    if not thread_messages:
        return {
            'first_message': None,
            'message_count': 0,
            'first_message_text': '',
            'thread_starter': '',
            'last_message_time': None,
            'error': 'Could not retrieve thread messages'
        }
    
    first_message = thread_messages[0]
    last_message = thread_messages[-1]  # Messages are sorted chronologically
    
    return {
        'first_message': first_message,
        'message_count': len(thread_messages),
        'first_message_text': first_message.get('text', ''),
        'thread_starter': first_message.get('sender', {}).get('displayName', 'Unknown'),
        'last_message_time': last_message.get('createTime'),
        'error': None
    }

def get_people(service, spaces: List[Dict], start_date: str = None, end_date: str = None) -> List[str]:
    """Retrieve a list of unique people from SPACE type spaces by scraping messages."""
    people = set()
    for space in spaces:
        if space.get('spaceType') != 'SPACE':
            continue

        logging.info(f"Processing space: {space['name']}")
        
        try:
            messages = get_messages_for_space(service, space['name'], start_date, end_date)
            for message in messages:
                if 'sender' in message and 'displayName' in message['sender']:
                    people.add(message['sender']['displayName'])

                if 'text' in message and 'via Tasks' in message['text']:
                    text = message['text']
                    if "@" in text:
                        assignee = text.split("@")[1].split("(")[0].strip()
                        assignee = assignee.split(" to")[0].strip()
                        people.add(assignee)
        except Exception as e:
            logging.error(f"Error processing space {space['name']}: {e}")
            continue

    return list(people)

def get_tasks(service, space_name: str, start_date: str, end_date: str, thread_mode: str = "context", assignee_filter: str = None) -> List[Dict]:
    """Retrieve tasks from a specific space within a date range using a valid filter query.
    
    Args:
        service: Google Chat API service instance
        space_name: Name of the Chat space
        start_date: Start date in RFC 3339 format
        end_date: End date in RFC 3339 format
        thread_mode: Thread information to include: "context" (default, includes first message) or "full" (complete thread)
        assignee_filter: Optional glob pattern to filter tasks by assignee (avoids fetching messages for non-matching tasks)
    """
    tasks = []
    completed_tasks, reopened_tasks, deleted_tasks, assigned_tasks = set(), set(), set(), set()
    
    try:
        messages = get_messages_for_space(service, space_name, start_date, end_date)
        
        # Count tasks for progress tracking
        task_creation_messages = [msg for msg in messages if 'via Tasks' in msg.get('text', '') and "Created" in msg.get('text', '')]
        total_tasks = len(task_creation_messages)
        
        if total_tasks > 0:
            if thread_mode == "full":
                logging.info(f"Fetching complete thread messages for {total_tasks} tasks")
            else:
                logging.info(f"Fetching task context for {total_tasks} tasks")
        
        task_counter = 0
        for message in messages:
            if 'via Tasks' in message.get('text', ''):
                task_id = message['thread']['name'].split("/")[3]
                text = message['text']
                assignee = text.split("@")[1].split("(")[0].strip() if "@" in text else "Unassigned"
                thread_name = message.get('thread', {}).get('name', '')

                if "Created" in text:
                    task_counter += 1
                    
                    # Skip if assignee filter is provided and doesn't match
                    if assignee_filter and not matches_assignee_pattern(assignee, assignee_filter):
                        continue
                    
                    # Base task data
                    task_data = {
                        'id': task_id,
                        'assignee': assignee,
                        'status': 'OPEN',
                        'created_time': message['createTime'],
                        'space_name': space_name,
                        'message_text': message.get('text', ''),
                        'sender': message.get('sender', {}).get('displayName', 'Unknown'),
                        'thread_name': thread_name,
                    }
                    
                    # Always include basic thread context (first message for task understanding)
                    if task_counter % 10 == 0:
                        logging.info(f"Processing tasks: {task_counter}/{total_tasks}")
                    
                    # Get first message for context (lightweight - only need first message)
                    first_message = get_first_thread_message(service, space_name, thread_name)
                    task_data['first_thread_message'] = first_message.get('text', '') if first_message else ''
                    
                    # Add complete thread messages only if requested
                    if thread_mode == "full":
                        thread_messages = get_thread_messages(service, space_name, thread_name)
                        # Simplify thread messages to just the essential information
                        simplified_messages = []
                        for msg in thread_messages:
                            simplified_messages.append({
                                'date': msg.get('createTime', ''),
                                'sender': msg.get('sender', {}).get('displayName', 'Unknown'),
                                'message': msg.get('text', '')
                            })
                        task_data['thread_messages'] = simplified_messages
                    
                    tasks.append(task_data)
                elif "Assigned" in text:
                    assigned_tasks.add(task_id + "@" + assignee)
                elif "Completed" in text:
                    completed_tasks.add(task_id)
                elif "Deleted" in text:
                    deleted_tasks.add(task_id)
                elif "Re-opened" in text:
                    reopened_tasks.add(task_id)

    except Exception as e:
        logging.error(f"Error fetching tasks from space {space_name}: {e}")
        raise

    # Update task statuses
    for task in tasks:
        task_id = task['id']
        if task_id in deleted_tasks:
            tasks.remove(task)

        for assigned in assigned_tasks:
            new_assignment = assigned.split("@")
            tid = new_assignment[0]
            t_assignee = new_assignment[1]

            if tid == task['id']:
                task['assignee'] = t_assignee
                continue

        if task_id in completed_tasks:
            task['status'] = 'COMPLETED'
        elif task_id in reopened_tasks:
            task['status'] = 'OPEN'

    # Apply IGNORE_ASSIGNEE filtering
    ignore_assignee_env = os.environ.get('IGNORE_ASSIGNEE', '')
    if ignore_assignee_env:
        try:
            ignored_assignees = json.loads(ignore_assignee_env)
            original_count = len(tasks)
            tasks = [t for t in tasks if t.get('assignee', '').strip() not in ignored_assignees]
            if original_count > len(tasks):
                logging.info(f"Filtered {original_count - len(tasks)} tasks via IGNORE_ASSIGNEE")
        except json.JSONDecodeError as e:
            logging.error(f"Error parsing IGNORE_ASSIGNEE: {e}")

    return tasks

def analyze_tasks(tasks: List[Dict]) -> List[Dict]:
    """Analyze tasks and generate a report with tasks received, completed, and completion rate."""
    if not tasks:
        logging.warning("No tasks found to analyze.")
        return []

    # Count tasks received and completed per assignee using dictionaries
    tasks_received = defaultdict(int)
    tasks_completed = defaultdict(int)
    
    for task in tasks:
        assignee = task.get('assignee', 'Unknown')
        tasks_received[assignee] += 1
        if task.get('status') == 'COMPLETED':
            tasks_completed[assignee] += 1
    
    # Build report as list of dictionaries
    report = []
    for assignee in sorted(tasks_received.keys()):
        received = tasks_received[assignee]
        completed = tasks_completed[assignee]
        completion_rate = completed / received if received > 0 else 0.0
        
        report.append({
            'assignee': assignee,
            'tasks_received': received,
            'tasks_completed': completed,
            'completion_rate': completion_rate
        })
    
    return report

def generate_report(report: List[Dict], start_date: str, end_date: str, filename: str):
    """Generate and save the task report as a CSV file."""
    # Convert dates to ISO format for display
    start_iso = datetime.fromisoformat(start_date.replace('Z', '')).strftime('%Y-%m-%d')
    end_iso = datetime.fromisoformat(end_date.replace('Z', '')).strftime('%Y-%m-%d')
    
    # Use the provided filename
    file_name = filename
    
    # Save report using our csv-based function
    save_to_csv(report, file_name)
    
    # Print date range and report
    logging.info(f"\nTask Report for period: {start_iso} to {end_iso}")
    for row in report:
        logging.info(f"  {row['assignee']}: {row['tasks_received']} received, {row['tasks_completed']} completed, {row['completion_rate']:.1%} completion rate")
    logging.info(f"\nReport saved as {file_name}")

def get_default_dates():
    """Get the default date range for the previous calendar month in RFC 3339 format."""
    today = datetime.today()
    first_day_of_month = today.replace(day=1)
    last_day_of_previous_month = first_day_of_month - timedelta(days=1)
    first_day_of_previous_month = last_day_of_previous_month.replace(day=1)
    return (
        first_day_of_previous_month.isoformat() + "Z",  # Start date
        last_day_of_previous_month.isoformat() + "Z"    # End date
    )

def get_past_day_dates():
    """Get the date range for the past day (1 day ago to today) in RFC 3339 format."""
    today = datetime.today()
    past_day = today - timedelta(days=1)
    return (
        past_day.isoformat() + "Z",  # Start date
        today.isoformat() + "Z"      # End date
    )

def get_past_month_dates():
    """Get the date range for the past month (30 days ago to today) in RFC 3339 format."""
    today = datetime.today()
    past_month = today - timedelta(days=30)
    return (
        past_month.isoformat() + "Z",  # Start date
        today.isoformat() + "Z"        # End date
    )

def get_past_week_dates():
    """Get the date range for the past week (7 days ago to today) in RFC 3339 format."""
    today = datetime.today()
    past_week = today - timedelta(days=7)
    return (
        past_week.isoformat() + "Z",  # Start date
        today.isoformat() + "Z"       # End date
    )

def get_past_year_dates():
    """Get the date range for the past year (365 days ago to today) in RFC 3339 format."""
    today = datetime.today()
    past_year = today - timedelta(days=365)
    return (
        past_year.isoformat() + "Z",  # Start date
        today.isoformat() + "Z"       # End date
    )

def convert_to_rfc3339(date_str: str) -> str:
    """Convert an ISO format date (e.g., 2022-01-15) to RFC 3339 format (e.g., 2022-01-15T00:00:00Z)."""
    try:
        # Parse the input date string
        date_obj = datetime.fromisoformat(date_str)
        # Convert to RFC 3339 format
        return date_obj.isoformat() + "Z"
    except ValueError:
        raise ValueError(f"Invalid date format: {date_str}. Expected format: YYYY-MM-DD")

def parse_date_range(args) -> Tuple[str, str]:
    """
    Parse date range arguments and return start and end dates in RFC 3339 format.
    
    Args:
        args: Command line arguments object containing date-related flags
        
    Returns:
        Tuple: (date_start, date_end) in RFC 3339 format
        
    Raises:
        ValueError: If date parsing fails or invalid date combination is provided
    """
    try:
        # Handle date range options with priority: past-day > past-week > past-month/past-year > custom dates > default dates
        if hasattr(args, 'past_day') and args.past_day:
            date_start, date_end = get_past_day_dates()
            logging.info("Using past day date range (1 day ago to today)")
        elif hasattr(args, 'past_week') and args.past_week:
            date_start, date_end = get_past_week_dates()
            logging.info("Using past week date range (7 days ago to today)")
        elif hasattr(args, 'past_month') and args.past_month:
            date_start, date_end = get_past_month_dates()
            logging.info("Using past month date range (30 days ago to today)")
        elif hasattr(args, 'past_year') and args.past_year:
            date_start, date_end = get_past_year_dates()
            logging.info("Using past year date range (365 days ago to today)")
        elif hasattr(args, 'date_start') and hasattr(args, 'date_end') and args.date_start and args.date_end:
            date_start = convert_to_rfc3339(args.date_start)
            date_end = convert_to_rfc3339(args.date_end)
        elif hasattr(args, 'date_start') and hasattr(args, 'date_end') and (args.date_start or args.date_end):
            logging.error("Both --date-start and --date-end must be provided together")
            raise ValueError("Both --date-start and --date-end must be provided together")
        else:
            date_start, date_end = get_default_dates()
            logging.info("Using default date range (previous calendar month)")
        
        return date_start, date_end
    except Exception as e:
        if isinstance(e, ValueError):
            raise
        logging.error(f"Error parsing date range: {e}")
        raise ValueError(f"Error parsing date range: {e}")

def format_task_info(task: Dict, space_name: str, thread_mode: str = "context") -> Dict:
    """Format task information in a human-friendly way."""
    formatted_task = {
        'id': task['id'],
        'assignee': task.get('assignee', 'Unassigned'),
        'status': task.get('status', 'UNKNOWN'),
        'space': space_name,
        'created_at': task.get('created_time'),
        'last_updated': task.get('last_update_time', task.get('created_time')),
        'message_text': task.get('message_text', ''),
        'sender': task.get('sender', ''),
        'thread_name': task.get('thread_name', ''),
        # Always include first message for context
        'first_thread_message': task.get('first_thread_message', ''),
    }
    
    # Add complete thread messages only if requested
    if thread_mode == "full":
        formatted_task['thread_messages'] = task.get('thread_messages', [])
    
    return formatted_task

def get_formatted_tasks(service, spaces: List[Dict], start_date: str = None, end_date: str = None, thread_mode: str = "context") -> List[Dict]:
    """Retrieve formatted task information from specified spaces."""
    formatted_tasks = []
    
    for space in spaces:
        space_name = space.get('displayName', space['name'])
        logging.info(f"Fetching tasks from space: {space_name}")
        
        try:
            tasks = get_tasks(service, space['name'], start_date, end_date, thread_mode)
            for task in tasks:
                formatted_task = format_task_info(task, space_name, thread_mode)
                formatted_tasks.append(formatted_task)
        except Exception as e:
            logging.error(f"Error fetching tasks from space {space_name}: {e}")
            continue
            
    return formatted_tasks

# REMOVED: Google Tasks API functions
# See GOOGLE_CHAT_TASKS_LIMITATIONS.md for explanation
# Tasks created via Google Chat are NOT accessible through Google Tasks API

def get_tasks_for_assignee(service, space_name: str, assignee_name: str, start_date: str, end_date: str, creds: Credentials = None, thread_mode: str = "context") -> List[Dict]:
    """
    Retrieve tasks assigned to a specific person using only Google Chat API.
    
    IMPORTANT LIMITATION: This function only uses Google Chat API because tasks created
    via Google Chat are NOT accessible through Google Tasks API. See 
    GOOGLE_CHAT_TASKS_LIMITATIONS.md for detailed explanation.
    
    What this function CAN track:
    - Task creation events from Chat messages
    - Task assignment changes
    - Task completion notifications  
    - Task deletion notifications
    - Basic task metadata (assignee, creation time, space)
    
    What this function CANNOT track:
    - Task titles and descriptions (only generic "Created a task for @Person")
    - Due dates
    - Detailed task status from Tasks API
    - Task notes and comments
    - Task list organization
    
    Args:
        service: Google Chat API service instance
        space_name: Name of the Chat space to search
        assignee_name: Name of the person to filter tasks for
        start_date: Start date in RFC 3339 format
        end_date: End date in RFC 3339 format
        creds: Credentials object (unused - kept for compatibility)
        
    Returns:
        List of task dictionaries with available information from Chat messages
    """
    tasks = []
    
    try:
        messages = get_messages_for_space(service, space_name, start_date, end_date)
        
        # First pass: collect all task-related messages
        task_messages = {}
        
        for message in messages:
            if 'via Tasks' in message.get('text', ''):
                task_id = message['thread']['name'].split("/")[3]
                text = message['text']
                create_time = message['createTime']
                sender = message.get('sender', {}).get('displayName', 'Unknown')
                
                if task_id not in task_messages:
                    task_messages[task_id] = {
                        'created': None,
                        'assigned': [],
                        'completed': [],
                        'reopened': [],
                        'deleted': [],
                        'updates': []
                    }
                
                # Categorize messages based on Chat message patterns
                if "Created" in text:
                    assignee = text.split("@")[1].split("(")[0].strip() if "@" in text else "Unassigned"
                    task_messages[task_id]['created'] = {
                        'assignee': assignee,
                        'time': create_time,
                        'text': text,
                        'sender': sender
                    }
                elif "Assigned" in text:
                    assignee = text.split("@")[1].split("(")[0].strip() if "@" in text else "Unassigned"
                    task_messages[task_id]['assigned'].append({
                        'assignee': assignee,
                        'time': create_time,
                        'text': text,
                        'sender': sender
                    })
                elif "Completed" in text:
                    task_messages[task_id]['completed'].append({
                        'time': create_time,
                        'text': text,
                        'sender': sender
                    })
                elif "Deleted" in text:
                    task_messages[task_id]['deleted'].append({
                        'time': create_time,
                        'text': text,
                        'sender': sender
                    })
                elif "Re-opened" in text:
                    task_messages[task_id]['reopened'].append({
                        'time': create_time,
                        'text': text,
                        'sender': sender
                    })
                else:
                    # General updates
                    task_messages[task_id]['updates'].append({
                        'time': create_time,
                        'text': text,
                        'sender': sender
                    })
        
        # Second pass: process tasks and find those assigned to the target person
        for task_id, task_data in task_messages.items():
            if not task_data['created']:
                continue
                
            # Check if task is assigned to the target person
            current_assignee = task_data['created']['assignee']
            
            # Check all assignment messages to find current assignee
            for assignment in task_data['assigned']:
                current_assignee = assignment['assignee']
            
            # Check if current assignee matches the pattern
            # Supports both exact matching and glob patterns (e.g., '*riyanka D*')
            if not matches_assignee_pattern(current_assignee, assignee_name):
                # Also try matching without the parenthetical part for backwards compatibility
                assignee_no_paren = assignee_name.split('(')[0].strip()
                if not matches_assignee_pattern(current_assignee, assignee_no_paren):
                    continue
            
            # Skip if task was deleted
            if task_data['deleted']:
                continue
            
            # Determine task status based on Chat messages
            status = 'OPEN'
            if task_data['completed']:
                # Check if it was reopened after completion
                latest_completion = max(task_data['completed'], key=lambda x: x['time'])
                latest_reopening = max(task_data['reopened'], key=lambda x: x['time']) if task_data['reopened'] else None
                
                if not latest_reopening or latest_reopening['time'] < latest_completion['time']:
                    status = 'COMPLETED'
            
            # Extract task name from Chat message (limited information available)
            # Chat messages only show generic "Created a task for @Person (via Tasks)"
            task_name = "Task created via Chat"
            task_description = ""
            
            # Try to extract any additional context from the creation message
            text = task_data['created']['text']
            if "Created" in text and ":" in text:
                # Look for task description after colon
                potential_desc = text.split("Created")[1].split(":")[1].strip()
                if "@" in potential_desc:
                    potential_desc = potential_desc.split("@")[0].strip()
                if potential_desc and potential_desc != "a task for":
                    task_name = potential_desc
            elif "Created" in text:
                # Look for task description after "Created"
                potential_desc = text.split("Created")[1].strip()
                if "@" in potential_desc:
                    potential_desc = potential_desc.split("@")[0].strip()
                if potential_desc and potential_desc != "a task for":
                    task_name = potential_desc
            
            # Look for due date mentions in updates (very limited)
            due_date = None
            for update in task_data['updates']:
                if "due" in update['text'].lower() or "deadline" in update['text'].lower():
                    # Try to extract date from the message
                    # This is a simple implementation - could be enhanced with better date parsing
                    due_date = update['time']  # Use message time as fallback
            
            # Find last update time
            last_update = task_data['created']['time']
            if task_data['updates']:
                last_update = max(task_data['updates'], key=lambda x: x['time'])['time']
            if task_data['assigned']:
                latest_assignment = max(task_data['assigned'], key=lambda x: x['time'])
                if latest_assignment['time'] > last_update:
                    last_update = latest_assignment['time']
            if task_data['completed']:
                latest_completion = max(task_data['completed'], key=lambda x: x['time'])
                if latest_completion['time'] > last_update:
                    last_update = latest_completion['time']
            if task_data['reopened']:
                latest_reopening = max(task_data['reopened'], key=lambda x: x['time'])
                if latest_reopening['time'] > last_update:
                    last_update = latest_reopening['time']
            
            # Find last progress from the assignee
            last_progress = None
            assignee_updates = [update for update in task_data['updates'] 
                              if matches_assignee_pattern(update['sender'], assignee_name)]
            if assignee_updates:
                last_progress = max(assignee_updates, key=lambda x: x['time'])['time']
            
            # Find assignment time
            assignment_time = task_data['created']['time']
            if task_data['assigned']:
                # Find the most recent assignment to this person
                assignee_assignments = [a for a in task_data['assigned'] 
                                      if matches_assignee_pattern(a['assignee'], assignee_name)]
                if assignee_assignments:
                    assignment_time = max(assignee_assignments, key=lambda x: x['time'])['time']
            
            # Base task info
            task_info = {
                'task_id': task_id,
                'task_name': task_name,
                'task_description': task_description,
                'assignee': current_assignee,
                'assignment_time': assignment_time,
                'due_date': due_date,
                'last_update': last_update,
                'last_progress': last_progress,
                'status': status,
                'space_name': space_name,
                'created_text': task_data['created']['text'],
                'created_sender': task_data['created']['sender'],
                'tasklist_title': '',  # Not available from Chat API
                'tasks_api_available': False,  # Always False - see limitations
                'timestamp_matched': False,  # Not applicable - no Tasks API integration
                'api_task_id': '',  # Not available - Chat and Tasks APIs are separate
            }
            
            # Always include first message for context
            thread_name = f"spaces/{space_name.split('/')[-1]}/threads/{task_id}"
            first_message = get_first_thread_message(service, space_name, thread_name)
            task_info['first_thread_message'] = first_message.get('text', '') if first_message else ''
            
            # Add complete thread messages only if requested
            if thread_mode == "full":
                thread_messages = get_thread_messages(service, space_name, thread_name)
                # Simplify thread messages to just the essential information
                simplified_messages = []
                for msg in thread_messages:
                    simplified_messages.append({
                        'date': msg.get('createTime', ''),
                        'sender': msg.get('sender', {}).get('displayName', 'Unknown'),
                        'message': msg.get('text', '')
                    })
                task_info['thread_messages'] = simplified_messages
            
            tasks.append(task_info)
            
    except Exception as e:
        logging.error(f"Error fetching tasks for assignee from space {space_name}: {e}")
        raise
    
    return tasks

def export_messages(service, space_name: str, start_date: str, end_date: str, output_format: str = "json", filename: str = None) -> None:
    """Export all chat messages from a specific space in the specified format."""
    logging.info(f"Exporting messages from space: {space_name}")
    
    try:
        # Get all messages from the specified space
        messages = get_messages_for_space(service, space_name, start_date, end_date)
        
        if not messages:
            logging.info("No messages found in the specified space and date range.")
            return
        
        # Format messages for export
        formatted_messages = []
        for message in messages:
            formatted_message = {
                'id': message.get('name', ''),
                'text': message.get('text', ''),
                'sender': message.get('sender', {}).get('displayName', 'Unknown'),
                'sender_id': message.get('sender', {}).get('name', ''),
                'space': space_name,
                'created_at': message.get('createTime', ''),
                'thread_name': message.get('thread', {}).get('name', ''),
                'message_type': message.get('messageType', ''),
                'deleted': message.get('deleted', False),
                'last_updated': message.get('lastUpdateTime', message.get('createTime', ''))
            }
            formatted_messages.append(formatted_message)
        
        # Save to the provided filename or display in terminal
        if filename:
            save_data(formatted_messages, filename, output_format.lower())
        else:
            # Display messages without saving if no filename provided
            import json
            print(json.dumps(formatted_messages, indent=4, ensure_ascii=False))
            logging.info(f"Found {len(formatted_messages)} messages (displayed, not saved)")
            
    except Exception as e:
        logging.error(f"Error exporting messages from space {space_name}: {e}")
        raise

def drill_down_report(service, tasks: List[Dict], date_start: str, date_end: str, assignee_pattern: str = None) -> Dict:
    """
    Generate a drill-down report with detailed task information for each assignee.
    
    For each assignee, shows:
    - Number of tasks assigned in the past week from the report start date
    - List of those tasks with their first message (not the "task created..." message)
    - Number of tasks closed in that week
    - List of closed tasks with their first message
    
    Args:
        service: Google Chat API service instance
        tasks: List of task dictionaries
        date_start: Report start date in RFC 3339 format
        date_end: Report end date in RFC 3339 format
        assignee_pattern: Optional glob pattern to filter assignees to show
        
    Returns:
        Dictionary mapping assignee names to their drill-down report data
    """
    # Calculate the date range for "past week" relative to report end date
    end_date_obj = datetime.fromisoformat(date_end.replace('Z', ''))
    week_start = end_date_obj - timedelta(days=7)
    week_start_str = week_start.isoformat() + "Z"
    
    # Group tasks by assignee
    assignee_data = {}
    
    for task in tasks:
        assignee = task.get('assignee', 'Unassigned')
        
        # If pattern is provided, only include assignees that match it
        if assignee_pattern and not matches_assignee_pattern(assignee, assignee_pattern):
            continue
        
        if assignee not in assignee_data:
            assignee_data[assignee] = {
                'tasks_assigned_this_week': [],
                'tasks_closed_this_week': [],
                'total_tasks': 0,
                'total_completed': 0
            }
        
        assignee_data[assignee]['total_tasks'] += 1
        if task.get('status') == 'COMPLETED':
            assignee_data[assignee]['total_completed'] += 1
        
        # Check if task was created in the past week
        created_time = task.get('created_time', '')
        if created_time:
            created_date = datetime.fromisoformat(created_time.replace('Z', ''))
            if created_date >= week_start:
                # Use existing first thread message from task data (already fetched during task collection)
                # This avoids unnecessary API calls
                first_message = task.get('first_thread_message', '')
                
                # Only fetch if not already available
                if not first_message:
                    thread_name = task.get('thread_name', '')
                    space_name = task.get('space_name', '')
                    
                    if thread_name and space_name:
                        try:
                            first_msg_data = get_first_thread_message(service, space_name, thread_name)
                            first_message = first_msg_data.get('text', '') if first_msg_data else ''
                        except Exception as e:
                            logging.warning(f"Could not retrieve first message for task {task.get('id')}: {e}")
                
                assignee_data[assignee]['tasks_assigned_this_week'].append({
                    'task_id': task.get('id'),
                    'created_time': created_time,
                    'first_message': first_message,
                    'space': task.get('space_name', ''),
                    'status': task.get('status', 'UNKNOWN')
                })
        
        # Check if task was completed in the past week
        # Note: We don't have completion_time in the current structure, 
        # so we'll check if the task is completed and was created recently
        # For a more accurate implementation, you'd need to track completion events separately
        if task.get('status') == 'COMPLETED' and created_time:
            created_date = datetime.fromisoformat(created_time.replace('Z', ''))
            if created_date >= week_start:
                # Use existing first thread message from task data (already fetched during task collection)
                # This avoids unnecessary API calls
                first_message = task.get('first_thread_message', '')
                
                # Only fetch if not already available
                if not first_message:
                    thread_name = task.get('thread_name', '')
                    space_name = task.get('space_name', '')
                    
                    if thread_name and space_name:
                        try:
                            first_msg_data = get_first_thread_message(service, space_name, thread_name)
                            first_message = first_msg_data.get('text', '') if first_msg_data else ''
                        except Exception as e:
                            logging.warning(f"Could not retrieve first message for task {task.get('id')}: {e}")
                
                assignee_data[assignee]['tasks_closed_this_week'].append({
                    'task_id': task.get('id'),
                    'created_time': created_time,
                    'first_message': first_message,
                    'space': task.get('space_name', ''),
                })
    
    return assignee_data

def drill_down_report_streaming(service, tasks: List[Dict], date_start: str, date_end: str, assignee_pattern: str = None) -> Dict:
    """
    Generate and print drill-down report in real-time as data is processed.
    Prints each assignee section immediately for streaming output.
    """
    # Calculate the date range for "past week" relative to report end date
    end_date_obj = datetime.fromisoformat(date_end.replace('Z', ''))
    week_start = end_date_obj - timedelta(days=7)
    
    # Group tasks by assignee (same logic as drill_down_report)
    assignee_data = {}
    
    for task in tasks:
        assignee = task.get('assignee', 'Unassigned')
        
        # If pattern is provided, only include assignees that match it
        if assignee_pattern and not matches_assignee_pattern(assignee, assignee_pattern):
            continue
        
        if assignee not in assignee_data:
            assignee_data[assignee] = {
                'tasks_assigned_this_week': [],
                'tasks_closed_this_week': [],
                'total_tasks': 0,
                'total_completed': 0
            }
        
        assignee_data[assignee]['total_tasks'] += 1
        if task.get('status') == 'COMPLETED':
            assignee_data[assignee]['total_completed'] += 1
        
        # Check if task was created in the past week
        created_time = task.get('created_time', '')
        if created_time:
            created_date = datetime.fromisoformat(created_time.replace('Z', ''))
            if created_date >= week_start:
                first_message = task.get('first_thread_message', '')
                
                assignee_data[assignee]['tasks_assigned_this_week'].append({
                    'task_id': task.get('id'),
                    'created_time': created_time,
                    'first_message': first_message,
                    'space': task.get('space_name', ''),
                    'status': task.get('status', 'UNKNOWN')
                })
        
        # Check if task was completed in the past week
        if task.get('status') == 'COMPLETED' and created_time:
            created_date = datetime.fromisoformat(created_time.replace('Z', ''))
            if created_date >= week_start:
                first_message = task.get('first_thread_message', '')
                
                assignee_data[assignee]['tasks_closed_this_week'].append({
                    'task_id': task.get('id'),
                    'created_time': created_time,
                    'first_message': first_message,
                    'space': task.get('space_name', ''),
                })
    
    # Print each assignee immediately as we process them
    for assignee, data in sorted(assignee_data.items()):
        print(f"\n{'─' * 80}")
        print(f"Assignee: {assignee}")
        print(f"{'─' * 80}")
        print(f"Total tasks in period: {data['total_tasks']}")
        print(f"Total completed: {data['total_completed']}")
        
        # Tasks assigned in the past week
        tasks_assigned_week = data['tasks_assigned_this_week']
        print(f"\n  Tasks assigned in past week: {len(tasks_assigned_week)}")
        if tasks_assigned_week:
            for i, task in enumerate(tasks_assigned_week, 1):
                created = datetime.fromisoformat(task['created_time'].replace('Z', '')).strftime('%Y-%m-%d %H:%M')
                print(f"    {i}. [{task['status']}] {created}")
                if task['first_message']:
                    # Truncate long messages
                    msg = task['first_message']
                    if len(msg) > 150:
                        msg = msg[:150] + "..."
                    print(f"       {msg}")
                print(f"       Space: {task['space']}")
                print()
        
        # Tasks closed in the past week
        tasks_closed_week = data['tasks_closed_this_week']
        print(f"  Tasks closed in past week: {len(tasks_closed_week)}")
        if tasks_closed_week:
            for i, task in enumerate(tasks_closed_week, 1):
                created = datetime.fromisoformat(task['created_time'].replace('Z', '')).strftime('%Y-%m-%d %H:%M')
                print(f"    {i}. [COMPLETED] {created}")
                if task['first_message']:
                    # Truncate long messages
                    msg = task['first_message']
                    if len(msg) > 150:
                        msg = msg[:150] + "..."
                    print(f"       {msg}")
                print(f"       Space: {task['space']}")
                print()
    
    return assignee_data

def list_spaces_interactive(spaces: List[Dict]) -> str:
    """List all spaces and let user choose one interactively."""
    print("\nAvailable spaces:")
    print("-" * 80)
    
    # Create a numbered list of spaces
    space_choices = []
    for i, space in enumerate(spaces, 1):
        space_id = space['name']
        display_name = space.get('displayName', space_id)
        space_type = space.get('spaceType', 'UNKNOWN')
        
        # Format space type for display
        if space_type == 'SPACE':
            type_indicator = '[PUBLIC]'
        elif space_type == 'DIRECT_MESSAGE':
            type_indicator = '[PRIVATE]'
        else:
            type_indicator = f'[{space_type}]'
        
        space_choices.append((i, space_id, display_name))
        print(f"{i:2d}. {type_indicator} {display_name}")
        print(f"    ID: {space_id}")
        print()
    
    # Get user choice
    while True:
        try:
            choice = input("Enter the number of the space to export messages from (or 'q' to quit): ").strip()
            
            if choice.lower() == 'q':
                print("Export cancelled.")
                return None
            
            choice_num = int(choice)
            if 1 <= choice_num <= len(space_choices):
                selected_space = space_choices[choice_num - 1][1]  # Get the space ID
                selected_name = space_choices[choice_num - 1][2]   # Get the display name
                print(f"\nSelected space: {selected_name}")
                return selected_space
            else:
                print(f"Please enter a number between 1 and {len(space_choices)}")
        except ValueError:
            print("Please enter a valid number or 'q' to quit")
        except KeyboardInterrupt:
            print("\nExport cancelled.")
            return None

def main():
    setup_logging()

    parser = argparse.ArgumentParser(
        description="Google Spaces Tasks Reporter - Analyse Google Chat spaces and extract task information",
        epilog="Use '<command> --help' for detailed help on any command"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands:")

    # Config command for token management
    config_parser = subparsers.add_parser(
        "config", 
        help="Configure authentication token",
        description="Configure or refresh your Google API authentication token. Required for first-time use and when tokens expire.",
        epilog="Example: python3 google_chat_reporter.py config"
    )

    # Spaces command
    spaces_parser = subparsers.add_parser(
        "spaces", 
        help="List accessible Google Chat spaces",
        description="List Google Chat spaces accessible to the authenticated account. By default, only shows public spaces; use flags to include direct messages.",
        epilog="""Examples:
  python3 google_chat_reporter.py spaces                           # List all public spaces
  python3 google_chat_reporter.py spaces --json spaces.json       # Save to JSON file
  python3 google_chat_reporter.py spaces --csv spaces.csv         # Save to CSV file
  python3 google_chat_reporter.py spaces --include-direct-messages # Include direct messages
  python3 google_chat_reporter.py spaces --all                    # Show all spaces and DMs"""
    )
    spaces_parser.add_argument("--json", metavar="FILE", 
                              help="Save the list of spaces to specified JSON file (preserves full data structure)")
    spaces_parser.add_argument("--csv", metavar="FILE", 
                              help="Save the list of spaces to specified CSV file (for spreadsheet analysis)")
    spaces_parser.add_argument("--include-direct-messages", action="store_true", 
                              help="Include direct message conversations in addition to public spaces")
    spaces_parser.add_argument("--all", action="store_true", 
                              help="Show all spaces including both public spaces and direct messages")

    # People command
    people_parser = subparsers.add_parser(
        "people", 
        help="List people found in spaces within a date range",
        description="Extract unique individuals who sent messages or were assigned tasks within spaces during a time period. Defaults to the previous calendar month if no date range is specified.",
        epilog="""Examples:
  python3 google_chat_reporter.py people                                        # Previous month
  python3 google_chat_reporter.py people --past-week                           # Past 7 days
  python3 google_chat_reporter.py people --past-month                          # Past 30 days
  python3 google_chat_reporter.py people --date-start 2024-01-01 --date-end 2024-01-31  # Custom range
  python3 google_chat_reporter.py people --json people.json                    # Save to JSON
  python3 google_chat_reporter.py people --csv people.csv                      # Save to CSV"""
    )
    people_parser.add_argument("--date-start", metavar="YYYY-MM-DD",
                              help="Start date in ISO format (e.g., 2024-01-15). Must be used with --date-end")
    people_parser.add_argument("--date-end", metavar="YYYY-MM-DD",
                              help="End date in ISO format (e.g., 2024-01-15). Must be used with --date-start")
    people_parser.add_argument("--past-week", action="store_true", 
                              help="Retrieve people from the past 7 days (from today)")
    people_parser.add_argument("--past-month", action="store_true", 
                              help="Retrieve people from the past 30 days (from today)")
    people_parser.add_argument("--past-year", action="store_true", 
                              help="Retrieve people from the past 365 days (from today)")
    people_parser.add_argument("--json", metavar="FILE", 
                              help="Save the list of people to specified JSON file")
    people_parser.add_argument("--csv", metavar="FILE", 
                              help="Save the list of people to specified CSV file")

    # Stats command - generates aggregate statistics
    stats_parser = subparsers.add_parser(
        "stats", 
        help="Generate summary statistics (completion rates per assignee)",
        description="Generate aggregate task completion statistics showing tasks received, completed, and completion rates per assignee. Use 'tasks' command for detailed individual task data. Defaults to the previous calendar month if no date range is specified.",
        epilog="""Examples:
  python3 google_chat_reporter.py stats                                       # Previous month stats
  python3 google_chat_reporter.py stats --past-week --csv weekly_stats.csv   # Past week to CSV
  python3 google_chat_reporter.py stats --past-month --json monthly.json     # Past month to JSON
  python3 google_chat_reporter.py stats --assignee "*ÐS" --drill-down        # Filter by name pattern with drill-down
  python3 google_chat_reporter.py stats --date-start 2024-01-01 --date-end 2024-01-31 --csv custom.csv"""
    )
    stats_parser.add_argument("--assignee", metavar="PATTERN",
                              help="Filter stats by assignee name. Supports glob patterns (e.g., '*ÐS' to match names ending with ÐS, '*john*' to match any john)")
    stats_parser.add_argument("--drill-down", action="store_true",
                              help="Drill down into per-assignee details including: tasks assigned in past week, tasks closed in past week, with task descriptions from first message")
    stats_parser.add_argument("--date-start", metavar="YYYY-MM-DD",
                              help="Start date in ISO format (e.g., 2024-01-15). Must be used with --date-end")
    stats_parser.add_argument("--date-end", metavar="YYYY-MM-DD",
                              help="End date in ISO format (e.g., 2024-01-15). Must be used with --date-start")
    stats_parser.add_argument("--past-week", action="store_true", 
                              help="Generate stats for the past 7 days (from today)")
    stats_parser.add_argument("--past-month", action="store_true", 
                              help="Generate stats for the past 30 days (from today)")
    stats_parser.add_argument("--past-year", action="store_true", 
                              help="Generate stats for the past 365 days (from today)")
    stats_parser.add_argument("--json", metavar="FILE", 
                              help="Save statistics to specified JSON file (preserves complete data)")
    stats_parser.add_argument("--csv", metavar="FILE", 
                              help="Save statistics to specified CSV file (suitable for spreadsheet analysis)")

    # Tasks command - exports detailed individual task data
    tasks_parser = subparsers.add_parser(
        "tasks", 
        help="Export detailed task data with thread context",
        description="Export individual task records with full details including assignee, status, timestamps, and thread context. Use 'stats' command for aggregate statistics. Supports filtering by assignee and includes thread messages for context. Defaults to the previous calendar month if no date range is specified.",
        epilog="""Examples:
  python3 google_chat_reporter.py tasks                                        # All tasks, previous month
  python3 google_chat_reporter.py tasks --assignee "John Doe"                 # Tasks for specific person
  python3 google_chat_reporter.py tasks --assignee "*john*"                   # Tasks matching pattern
  python3 google_chat_reporter.py tasks --space "spaces/ABC123"               # Tasks from specific space
  python3 google_chat_reporter.py tasks --past-week --with-threads --json tasks.json  # Past week with full threads
  python3 google_chat_reporter.py tasks --past-month --csv tasks.csv          # Past month to CSV"""
    )
    tasks_parser.add_argument("--assignee", metavar="NAME", 
                             help="Filter tasks by assignee name. Supports exact match (e.g., 'Priyanka D') or glob patterns with wildcards (e.g., '*riyanka*', '?riyanka D*')")
    tasks_parser.add_argument("--space", metavar="SPACE_ID",
                             help="Space ID to search in (e.g., 'spaces/ABC123'). If not provided, searches all accessible spaces")
    tasks_parser.add_argument("--date-start", metavar="YYYY-MM-DD",
                             help="Start date in ISO format (e.g., 2024-01-15). Must be used with --date-end")
    tasks_parser.add_argument("--date-end", metavar="YYYY-MM-DD",
                             help="End date in ISO format (e.g., 2024-01-15). Must be used with --date-start")
    tasks_parser.add_argument("--past-day", action="store_true", 
                             help="Retrieve tasks from the past 1 day (from today)")
    tasks_parser.add_argument("--past-week", action="store_true", 
                             help="Retrieve tasks from the past 7 days (from today)")
    tasks_parser.add_argument("--past-month", action="store_true", 
                             help="Retrieve tasks from the past 30 days (from today)")
    tasks_parser.add_argument("--past-year", action="store_true", 
                             help="Retrieve tasks from the past 365 days (from today)")
    tasks_parser.add_argument("--with-threads", action="store_true", 
                             help="Include complete thread messages for full context (JSON output only, not compatible with CSV due to nested structure)")
    tasks_parser.add_argument("--json", metavar="FILE", 
                             help="Save tasks to specified JSON file (supports both basic and full thread data)")
    tasks_parser.add_argument("--csv", metavar="FILE", 
                             help="Save tasks to specified CSV file (includes task context but not full threads)")

    # Messages command - auxiliary feature for raw message export
    messages_parser = subparsers.add_parser(
        "messages", 
        help="Export raw chat messages (auxiliary - not task-specific)",
        description="Export all chat messages from spaces or direct messages. This is an auxiliary feature for general message archival; it does not filter for tasks. For task-specific exports, use 'tasks' or 'stats' commands instead. Defaults to the previous calendar month if no date range is specified.",
        epilog="""Examples:
  python3 google_chat_reporter.py messages                                     # Interactive space selection
  python3 google_chat_reporter.py messages --space "spaces/ABC123" --json     # Specific space to JSON
  python3 google_chat_reporter.py messages --all-spaces --csv                 # All public spaces to CSV
  python3 google_chat_reporter.py messages --all-direct-messages --json       # All DMs to JSON
  python3 google_chat_reporter.py messages --all --csv                        # Everything to CSV
  python3 google_chat_reporter.py messages --past-week --json weekly_msgs.json # Past week to JSON"""
    )
    
    # Mutually exclusive group for space selection
    group = messages_parser.add_mutually_exclusive_group()
    group.add_argument("--space", metavar="SPACE_ID",
                      help="Space ID to export from (e.g., 'spaces/ABC123'). If not provided, shows interactive selection menu")
    group.add_argument("--all-spaces", action="store_true", 
                      help="Export messages from all public spaces (excludes direct messages)")
    group.add_argument("--all-direct-messages", action="store_true", 
                      help="Export messages from all direct message conversations (excludes public spaces)")
    group.add_argument("--all", action="store_true", 
                      help="Export messages from all spaces and direct messages (comprehensive export)")
    
    messages_parser.add_argument("--date-start", metavar="YYYY-MM-DD",
                                help="Start date in ISO format (e.g., 2024-01-15). Must be used with --date-end")
    messages_parser.add_argument("--date-end", metavar="YYYY-MM-DD",
                                help="End date in ISO format (e.g., 2024-01-15). Must be used with --date-start")
    messages_parser.add_argument("--past-week", action="store_true", 
                                help="Export messages from the past 7 days (from today)")
    messages_parser.add_argument("--past-month", action="store_true", 
                                help="Export messages from the past 30 days (from today)")
    messages_parser.add_argument("--past-year", action="store_true", 
                                help="Export messages from the past 365 days (from today)")
    messages_parser.add_argument("--json", metavar="FILE", 
                                help="Save the exported messages to specified JSON file (preserves full message structure)")
    messages_parser.add_argument("--csv", metavar="FILE", 
                                help="Save the exported messages to specified CSV file (suitable for spreadsheet analysis)")

    # Thread command - auxiliary feature for single thread inspection
    thread_parser = subparsers.add_parser(
        "thread", 
        help="Retrieve messages from a specific thread (auxiliary)",
        description="Retrieve all messages from a specific thread within a Google Chat space. This is an auxiliary feature for inspecting individual conversation threads; for task-related exports, use 'tasks' command instead.",
        epilog="""Examples:
  python3 google_chat_reporter.py thread --space "spaces/ABC123" --thread "spaces/ABC123/threads/XYZ789" --json
  python3 google_chat_reporter.py thread --space "spaces/ABC123" --thread "spaces/ABC123/threads/XYZ789" --csv
  python3 google_chat_reporter.py thread --space "spaces/ABC123" --thread "spaces/ABC123/threads/XYZ789"  # Display only"""
    )
    thread_parser.add_argument("--space", metavar="SPACE_ID", required=True, 
                              help="Space ID containing the thread (e.g., 'spaces/ABC123')")
    thread_parser.add_argument("--thread", metavar="THREAD_ID", required=True, 
                              help="Full thread ID to retrieve messages from (e.g., 'spaces/ABC123/threads/XYZ789')")
    thread_parser.add_argument("--json", metavar="FILE", 
                              help="Save thread messages to specified JSON file (preserves full message structure)")
    thread_parser.add_argument("--csv", metavar="FILE", 
                              help="Save thread messages to specified CSV file (suitable for spreadsheet analysis)")

    args = parser.parse_args()

    # If no command is provided, show help
    if not args.command:
        parser.print_help()
        print("\nAvailable commands:")
        print("  config   - Configure authentication token")
        print("  spaces   - List accessible Google Chat spaces")
        print("  people   - List people found in spaces within a date range")
        print("")
        print("  Task-related commands:")
        print("  tasks    - Export detailed task data with thread context")
        print("  stats    - Generate summary statistics (completion rates per assignee)")
        print("")
        print("  Auxiliary commands (not task-specific):")
        print("  messages - Export raw chat messages from spaces")
        print("  thread   - Retrieve all messages from a specific thread")
        print("\nUse '<command> --help' for detailed options.")
        return

    # Only get credentials when a command is actually provided
    if args.command == "config":
        print("Configuring authentication token...")
        creds = get_credentials()
        print("Authentication token configured successfully!")
        return

    # For all other commands, get credentials and build service
    creds = get_credentials()
    service = build('chat', 'v1', credentials=creds)

    if args.command == "spaces":
        if args.all:
            spaces = get_all_spaces_and_dms(service)
            logging.info("Retrieved all spaces including direct messages")
        elif args.include_direct_messages:
            public_spaces = get_public_spaces(service)
            dm_spaces = get_direct_message_spaces(service)
            spaces = public_spaces + dm_spaces
            logging.info("Retrieved public spaces and direct message conversations")
        else:
            spaces = get_spaces(service)
            logging.info("Retrieved public spaces only")
        
        if args.json:
            save_data(spaces, args.json, "json")
        elif args.csv:
            save_data(spaces, args.csv, "csv")
        else:
            # Output space ID and space name in format "space id : space name"
            for space in spaces:
                space_id = space['name']
                if space.get('spaceType') == 'DIRECT_MESSAGE':
                    space_name = "Direct Message"
                else:
                    space_name = space.get('displayName', space_id)
                print(f"{space_id} : {space_name}")

    elif args.command == "people":
        try:
            date_start, date_end = parse_date_range(args)
        except ValueError as e:
            logging.error(e)
            return

        spaces = get_spaces(service)
        people = get_people(service, spaces, date_start, date_end)
        if args.json:
            save_data(people, args.json, "json")
        elif args.csv:
            save_data(people, args.csv, "csv")
        else:
            print(json.dumps(people, indent=4, ensure_ascii=False))

    elif args.command == "stats":
        try:
            date_start, date_end = parse_date_range(args)
        except ValueError as e:
            logging.error(e)
            return

        # Always fetch fresh data based on user's date parameters
        logging.info(f"Fetching tasks from API for date range: {date_start} to {date_end}")
        spaces = get_spaces(service)

        # Get assignee filter if specified (pass to get_tasks to avoid fetching unnecessary data)
        assignee_filter = args.assignee if hasattr(args, 'assignee') and args.assignee else None
        
        # Fetch tasks for the specified date range (with early filtering for efficiency)
        all_tasks = []
        for space in spaces:
            tasks = get_tasks(service, space['name'], date_start, date_end, "context", assignee_filter)
            all_tasks.extend(tasks)
        
        if assignee_filter:
            logging.info(f"Found {len(all_tasks)} tasks matching assignee pattern: {assignee_filter}")

        if not all_tasks:
            logging.info("No tasks found matching the criteria")
            return

        # Generate drill-down report if requested
        if hasattr(args, 'drill_down') and args.drill_down:
            logging.info("Generating drill-down report with per-assignee task breakdown...")
            # Pass assignee pattern to drill-down report for additional filtering
            assignee_pattern = args.assignee if hasattr(args, 'assignee') else None
            
            # Convert dates to ISO format for display
            start_iso = datetime.fromisoformat(date_start.replace('Z', '')).strftime('%Y-%m-%d')
            end_iso = datetime.fromisoformat(date_end.replace('Z', '')).strftime('%Y-%m-%d')
            
            # Print header immediately
            print(f"\n{'=' * 80}")
            print(f"TASK REPORT - DRILL-DOWN VIEW")
            print(f"Period: {start_iso} to {end_iso}")
            print(f"{'=' * 80}\n")
            
            # Stream output as we process (for real-time feedback)
            drill_down_data = drill_down_report_streaming(service, all_tasks, date_start, date_end, assignee_pattern)
            
            print(f"\n{'=' * 80}\n")
            
            # Optionally save drill-down data to JSON
            if args.json:
                save_data(drill_down_data, args.json, "json")
                logging.info(f"Drill-down report saved as {args.json}")
        else:
            # Generate standard report
            report = analyze_tasks(all_tasks)
            if args.json:
                # Report is already a list of dicts, ready for JSON output
                save_data(report, args.json, "json")
                logging.info(f"Report saved as {args.json}")
            elif args.csv:
                # Use specified CSV filename for report generation
                generate_report(report, date_start, date_end, args.csv)
            else:
                # Convert dates to ISO format for display
                start_iso = datetime.fromisoformat(date_start.replace('Z', '')).strftime('%Y-%m-%d')
                end_iso = datetime.fromisoformat(date_end.replace('Z', '')).strftime('%Y-%m-%d')
                print(f"\nTask Report for period: {start_iso} to {end_iso}")
                # Format report as table
                if report:
                    # Print header
                    print(f"{'Assignee':<30} {'Tasks Received':<15} {'Tasks Completed':<17} {'Completion Rate':<15}")
                    print("-" * 80)
                    # Print data rows
                    for row in report:
                        assignee = row['assignee'][:29] if len(row['assignee']) > 29 else row['assignee']
                        print(f"{assignee:<30} {row['tasks_received']:<15} {row['tasks_completed']:<17} {row['completion_rate']:<14.1%}")
                else:
                    print("No tasks found in the report period.")

    elif args.command == "tasks":
        try:
            date_start, date_end = parse_date_range(args)
        except ValueError as e:
            logging.error(e)
            return

        # Handle thread flag validation
        if args.csv and args.with_threads:
            logging.error("❌ CSV format is not compatible with complete thread messages due to nested data structure.")
            logging.error("💡 Suggestion: Use --json instead for complete thread messages, or use CSV for task summaries.")
            logging.error("   Examples:")
            logging.error("     python3 google_chat_reporter.py tasks --with-threads --json")
            logging.error("     python3 google_chat_reporter.py tasks --csv  # includes task context by default")
            return
        
        # Determine thread mode: always include basic context, optionally include full threads
        thread_mode = "full" if args.with_threads else "context"

        # Load spaces from file or fetch all spaces
        if args.space:
            # Search in specific space
            spaces = [{'name': args.space, 'displayName': args.space}]
        else:
            spaces = get_spaces(service)
        
        if args.assignee:
            # Filter tasks by assignee
            assignee_name = args.assignee
            logging.info(f"Searching for tasks assigned to: {assignee_name}")

            all_tasks = []
            for space in spaces:
                space_name = space.get('displayName', space['name'])
                logging.info(f"Searching in space: {space_name}")
                
                try:
                    tasks = get_tasks_for_assignee(service, space['name'], assignee_name, date_start, date_end, creds, thread_mode)
                    all_tasks.extend(tasks)
                    logging.info(f"Found {len(tasks)} tasks for {assignee_name} in {space_name}")
                except Exception as e:
                    logging.error(f"Error searching tasks in space {space_name}: {e}")
                    continue

            if not all_tasks:
                logging.info(f"No tasks found assigned to {assignee_name}")
                if args.json or args.csv:
                    logging.info("❌ No file was created because no matching tasks were found.")
                return

            # Format output
            if args.json:
                save_data(all_tasks, args.json, "json")
                logging.info(f"Saved {len(all_tasks)} tasks to {args.json}")
            elif args.csv:
                save_data(all_tasks, args.csv, "csv")
                logging.info(f"Saved {len(all_tasks)} tasks to {args.csv}")
            else:
                # Display results in terminal
                print(f"\nTasks assigned to {assignee_name}:")
                print("=" * 80)
                
                for i, task in enumerate(all_tasks, 1):
                    print(f"\n{i}. Task: {task['task_name']}")
                    if task['task_description']:
                        print(f"   Description: {task['task_description']}")
                    print(f"   Chat Task ID: {task['task_id']}")
                    print(f"   Assignee: {task['assignee']}")
                    print(f"   Assignment Time: {task['assignment_time']}")
                    print(f"   Due Date: {task['due_date'] or 'Not specified'}")
                    print(f"   Last Update: {task['last_update']}")
                    print(f"   Last Progress from {assignee_name}: {task['last_progress'] or 'None'}")
                    print(f"   Status: {task['status']}")
                    print(f"   Space: {task['space_name']}")
                    print(f"   Created by: {task['created_sender']}")
                    
                    # New thread information
                    print(f"   Thread Messages: {task.get('thread_message_count', 0)}")
                    print(f"   Thread Started by: {task.get('thread_starter', 'Unknown')}")
                    if task.get('first_thread_message'):
                        # Truncate long messages for display
                        first_msg = task['first_thread_message']
                        if len(first_msg) > 100:
                            first_msg = first_msg[:100] + "..."
                        print(f"   First Thread Message: {first_msg}")
                    if task.get('thread_retrieval_error'):
                        print(f"   ⚠️  Thread Info Error: {task['thread_retrieval_error']}")
                    
                    print(f"   ⚠️  {task.get('limitation_note', '')}")
                    print("-" * 80)
                
                logging.info(f"Found {len(all_tasks)} tasks assigned to {assignee_name}")
        else:
            # Get all formatted tasks (original behavior)
            formatted_tasks = get_formatted_tasks(service, spaces, date_start, date_end, thread_mode)
            
            if args.json:
                save_data(formatted_tasks, args.json, "json")
                logging.info(f"Saved {len(formatted_tasks)} tasks to {args.json}")
            elif args.csv:
                save_data(formatted_tasks, args.csv, "csv")
                logging.info(f"Saved {len(formatted_tasks)} tasks to {args.csv}")
            else:
                print(json.dumps(formatted_tasks, indent=4, ensure_ascii=False))
                logging.info(f"Found {len(formatted_tasks)} tasks")

    elif args.command == "messages":
        try:
            date_start, date_end = parse_date_range(args)
        except ValueError as e:
            logging.error(e)
            return

        # Determine which spaces to export from
        if args.space:
            # Use the specified space (works for both public spaces and direct messages)
            space_name = args.space
            # Get all spaces to validate the specified space exists
            all_spaces = get_all_spaces_and_dms(service)
            space_exists = any(space['name'] == space_name for space in all_spaces)
            if not space_exists:
                logging.error(f"Space '{space_name}' not found. Use 'spaces' command to see available spaces.")
                return
            
            # Export from single space
            if args.json:
                export_messages(service, space_name, date_start, date_end, "json", args.json)
            elif args.csv:
                export_messages(service, space_name, date_start, date_end, "csv", args.csv)
            else:
                # Just display the messages without saving
                export_messages(service, space_name, date_start, date_end, "json", None)
        
        elif args.all_spaces:
            # Export from all public spaces
            logging.info("Exporting messages from all public spaces...")
            spaces = get_public_spaces(service)
            for space in spaces:
                space_name = space['name']
                space_display = space.get('displayName', space_name)
                logging.info(f"Exporting from public space: {space_display}")
                if args.json:
                    export_messages(service, space_name, date_start, date_end, "json", args.json)
                if args.csv:
                    export_messages(service, space_name, date_start, date_end, "csv", args.csv)
                else:
                    logging.info(f"Would export from {space_display} (use --json or --csv to actually export)")
        
        elif args.all_direct_messages:
            # Export from all direct message conversations
            logging.info("Exporting messages from all direct message conversations...")
            spaces = get_direct_message_spaces(service)
            for space in spaces:
                space_name = space['name']
                space_display = space.get('displayName', space_name)
                logging.info(f"Exporting from direct message: {space_display}")
                if args.json:
                    export_messages(service, space_name, date_start, date_end, "json", args.json)
                if args.csv:
                    export_messages(service, space_name, date_start, date_end, "csv", args.csv)
                else:
                    logging.info(f"Would export from {space_display} (use --json or --csv to actually export)")
        
        elif args.all:
            # Export from all spaces and direct messages
            logging.info("Exporting messages from all spaces and direct messages...")
            spaces = get_all_spaces_and_dms(service)
            for space in spaces:
                space_name = space['name']
                space_display = space.get('displayName', space_name)
                space_type = space.get('spaceType', 'UNKNOWN')
                logging.info(f"Exporting from {space_type.lower()}: {space_display}")
                if args.json:
                    export_messages(service, space_name, date_start, date_end, "json", args.json)
                if args.csv:
                    export_messages(service, space_name, date_start, date_end, "csv", args.csv)
                else:
                    logging.info(f"Would export from {space_display} (use --json or --csv to actually export)")
        
        else:
            # Interactive space selection (current functionality)
            spaces = get_spaces(service)
            space_name = list_spaces_interactive(spaces)
            if not space_name:
                return  # User cancelled
            
            # Export from selected space
            if args.json:
                export_messages(service, space_name, date_start, date_end, "json", args.json)
            elif args.csv:
                export_messages(service, space_name, date_start, date_end, "csv", args.csv)
            else:
                # Just display the messages without saving
                export_messages(service, space_name, date_start, date_end, "json", None)

    elif args.command == "thread":
        # Retrieve messages from a specific thread
        space_name = args.space
        thread_name = args.thread
        
        logging.info(f"Retrieving messages from thread: {thread_name}")
        logging.info(f"In space: {space_name}")
        
        try:
            # Get thread messages using the existing function
            messages = get_thread_messages(service, space_name, thread_name)
            
            if messages:
                # Format the messages for output
                formatted_messages = []
                for message in messages:
                    formatted_message = {
                        'id': message.get('name', ''),
                        'text': message.get('text', ''),
                        'sender': message.get('sender', {}).get('displayName', 'Unknown'),
                        'sender_id': message.get('sender', {}).get('name', ''),
                        'space': space_name,
                        'thread': thread_name,
                        'created_at': message.get('createTime', ''),
                        'message_type': message.get('messageType', ''),
                        'deleted': message.get('deleted', False),
                        'last_updated': message.get('lastUpdateTime', message.get('createTime', ''))
                    }
                    formatted_messages.append(formatted_message)
                
                # Output or save the messages
                if args.json:
                    save_data(formatted_messages, args.json, "json")
                    logging.info(f"Saved {len(formatted_messages)} messages to {args.json}")
                elif args.csv:
                    save_data(formatted_messages, args.csv, "csv")
                    logging.info(f"Saved {len(formatted_messages)} messages to {args.csv}")
                else:
                    # Print to console
                    print(json.dumps(formatted_messages, indent=4, ensure_ascii=False))
                    logging.info(f"Retrieved {len(formatted_messages)} messages from thread")
            else:
                logging.info("No messages found in the specified thread.")
                
        except Exception as e:
            logging.error(f"Error retrieving thread messages: {e}")
            if "503" in str(e) or "500" in str(e):
                logging.info("💡 This appears to be a temporary server error. Try again in a few minutes.")
            elif "404" in str(e):
                logging.error("❌ Thread or space not found. Please check your thread and space IDs.")
            else:
                logging.error("❌ An unexpected error occurred. Check your thread and space IDs.")

if __name__ == '__main__':
    main()
