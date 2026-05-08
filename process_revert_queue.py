#!/usr/bin/env python3
"""
OGF Revert Queue Processor
Automates processing of revert requests from the OpenGeofiction wiki revert queue.

Workflow:
1. Fetch the raw wiki queue page
2. Parse {{revert-please|...}} entries
3. For each entry: validate guard rails, download changesets, revert, update wiki
4. Replace {{revert-please}} with {{revert-complete}} on success/failure
"""

import re
import os
import sys
import subprocess
import tempfile
import shutil
import requests
from urllib.parse import urlencode
from datetime import datetime, timezone

# ── Configuration ──────────────────────────────────────────────────────────────

WIKI_BASE = "https://wiki.opengeofiction.net"
WIKI_API = f"{WIKI_BASE}/api.php"
QUEUE_PAGE = "OpenGeofiction:Revert queue"

BOT_CONTROL_URL = f"{WIKI_BASE}/index.php/User:Brothie?action=raw"

OGF_API_BASE = "https://opengeofiction.net/api/0.6/"

REVERT_SCRIPTS = os.path.expanduser("~/osm-revert-scripts")
OSMTOOLSRC = os.path.expanduser("~/.osmtoolsrc")

# Guard rails from the wiki page description
MIN_YEAR = 2026
MAX_CHANGESETS = 250

# OSMTools credentials (same .osmtoolsrc used by the Perl scripts)
def load_osmtoolsrc():
    """Parse .osmtoolsrc for credentials."""
    prefs = {}
    with open(OSMTOOLSRC) as f:
        for line in f:
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                key, val = line.split('=', 1)
                prefs[key.strip()] = val.strip()
    return prefs


# ── Wiki Interaction ───────────────────────────────────────────────────────────

class WikiClient:
    """Handles MediaWiki API authentication and editing."""

    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'OGFRevertBot/1.0 (OpenGeofiction automated revert processor)'
        })

    def login(self):
        """Log in to the wiki using MediaWiki's login API."""
        # Step 1: Get login token
        params = {
            'action': 'query',
            'meta': 'tokens',
            'type': 'login',
            'format': 'json'
        }
        r = self.session.get(WIKI_API, params=params)
        r.raise_for_status()
        token = r.json()['query']['tokens']['logintoken']

        # Step 2: Log in (try clientlogin first, fall back to legacy login)
        # Try clientlogin (MediaWiki 1.27+)
        params = {
            'action': 'clientlogin',
            'loginreturnurl': WIKI_BASE + '/',
            'format': 'json'
        }
        data = {
            'logintoken': token,
            'username': self.username,
            'password': self.password
        }
        r = self.session.post(WIKI_API, params=params, data=data)
        r.raise_for_status()
        result = r.json()

        status = result.get('clientlogin', {}).get('status', '')
        if status == 'PASS':
            print(f"  Wiki login successful (clientlogin)")
            return True
        elif status in ('UI', 'REDIRECT'):
            # May need additional steps (2FA, etc.)
            print(f"  Wiki login needs additional step: {status}")
            return False
        else:
            # Fall back to legacy login action
            print(f"  clientlogin returned '{status}', trying legacy login...")
            return self._legacy_login(token)

    def _legacy_login(self, token):
        """Fallback to legacy login API."""
        params = {'action': 'login', 'format': 'json'}
        data = {
            'lgname': self.username,
            'lgpassword': self.password,
            'lgtoken': token
        }
        r = self.session.post(WIKI_API, params=params, data=data)
        r.raise_for_status()
        result = r.json()
        status = result.get('login', {}).get('result', '')
        if status == 'Success':
            print(f"  Wiki login successful (legacy)")
            return True
        else:
            print(f"  Wiki login failed: {status}")
            return False

    def get_edit_token(self):
        """Get an edit (CSRF) token."""
        params = {
            'action': 'query',
            'meta': 'tokens',
            'format': 'json'
        }
        r = self.session.get(WIKI_API, params=params)
        r.raise_for_status()
        return r.json()['query']['tokens']['csrftoken']

    def get_page_content(self, title):
        """Get raw wiki page content."""
        params = {
            'action': 'query',
            'titles': title,
            'prop': 'revisions',
            'rvprop': 'content',
            'format': 'json'
        }
        r = self.session.get(WIKI_API, params=params)
        r.raise_for_status()
        pages = r.json()['query']['pages']
        for page_id, page in pages.items():
            if 'revisions' in page:
                return page['revisions'][0]['*']
        return None

    def edit_page(self, title, new_content, summary):
        """Edit a wiki page."""
        token = self.get_edit_token()
        params = {'action': 'edit', 'format': 'json'}
        data = {
            'title': title,
            'text': new_content,
            'summary': summary,
            'token': token,
            'bot': '1'
        }
        r = self.session.post(WIKI_API, params=params, data=data)
        r.raise_for_status()
        result = r.json()
        if 'error' in result:
            raise Exception(f"Wiki edit error: {result['error']}")
        return result.get('edit', {})

    def get_raw_page(self, title):
        """Get raw page content via the MediaWiki API (requires authenticated session)."""
        params = {
            'action': 'query',
            'titles': title,
            'prop': 'revisions',
            'rvprop': 'content',
            'format': 'json'
        }
        r = self.session.get(WIKI_API, params=params)
        r.raise_for_status()
        pages = r.json()['query']['pages']
        for page_id, page in pages.items():
            if 'revisions' in page:
                return page['revisions'][0]['*']
        return None


# ── Permission Gate ────────────────────────────────────────────────────────────

def check_bot_permission():
    """
    Check the bot control wiki page for {{permission|yes}}.
    Returns True if permission is granted, False otherwise.
    If the page cannot be loaded, assume no permission.
    """
    print("Checking bot control permission...")
    try:
        r = requests.get(BOT_CONTROL_URL, timeout=15, headers={
            "User-Agent": "OGFRevertBot/1.0",
            "Referer": "https://opengeofiction.net/",
        })
        raw = r.text
    except Exception as e:
        print(f"  WARNING: Could not load bot control page: {e}")
        return False

    if "{{permission|yes}}" in raw:
        print("  Bot permission granted")
        return True
    else:
        print("  Bot permission NOT granted (missing {{permission|yes}})")
        return False


# ── Queue Parsing ──────────────────────────────────────────────────────────────

# Pattern to match {{revert-please|...}} template calls
# Format: {{revert-please|MapperUserName|first_changeset|last_changeset|~~~~}}
REVERT_PLEASE_RE = re.compile(
    r'\{\{\s*revert-please\s*\|'       # {{revert-please|
    r'([^|]*)'                           # param 1: mapper username
    r'\|([^|]*)'                         # param 2: first changeset (may be blank)
    r'\|([^|]*)'                         # param 3: last changeset (may be blank)
    r'\|((?:(?!\}\}).)*?)'               # param 4: requester signature (anything until }})
    r'\}\}',                             # }}
    re.DOTALL
)

# Pattern to find the "Revert queue" section and track where new entries go
QUEUE_SECTION_RE = re.compile(
    r'(==\s*Revert queue\s*==\s*\n)'
    r'(.*?)'
    r'(\n==\s)',
    re.DOTALL
)


def parse_queue(content):
    """Parse the wiki page and extract pending revert requests."""
    requests = []

    # First, remove content inside <pre> blocks and HTML comments
    # to avoid matching template examples
    cleaned = re.sub(r'<pre[^>]*>.*?</pre>', '', content, flags=re.DOTALL)
    cleaned = re.sub(r'<!--.*?-->', '', cleaned, flags=re.DOTALL)

    # Only match lines that start with "* " (wiki list items)
    # This ensures we only match actual queue entries, not examples
    for line in cleaned.split('\n'):
        line = line.strip()
        if not line.startswith('* {{revert-please'):
            continue

        m = REVERT_PLEASE_RE.search(line)
        if m:
            mapper = m.group(1).strip()
            first_cs = m.group(2).strip()
            last_cs = m.group(3).strip()
            requester = m.group(4).strip()

            # Skip if mapper looks like a placeholder (contains uppercase/lowercase mix like "MapperUserName")
            if mapper in ('MapperUserName', ''):
                continue

            requests.append({
                'mapper': mapper,
                'first_cs': int(first_cs) if first_cs else None,
                'last_cs': int(last_cs) if last_cs else None,
                'requester': requester,
                'match': m,
                'original_text': m.group(0)
            })

    return requests


# ── OGF API ────────────────────────────────────────────────────────────────────

class OGFClient:
    """Interact with the OpenGeofiction OSM API."""

    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.auth = (username, password)
        self.session.headers.update({
            'User-Agent': 'OGFRevertBot/1.0'
        })

    def get_changeset(self, cs_id):
        """Get changeset metadata. Returns None if not found (404)."""
        r = self.session.get(f"{OGF_API_BASE}changeset/{cs_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.text

    def get_user_changesets(self, username, since_date, until_date=None):
        """Get list of changesets for a user in a time range. Username is normalized to lowercase."""
        if until_date is None:
            until_date = datetime.now(tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
        params = {
            'display_name': username.lower(),
            'time': f"{since_date},{until_date}"
        }
        r = self.session.get(f"{OGF_API_BASE}changesets", params=params)
        r.raise_for_status()
        return r.text

    def get_user_changesets_by_id_range(self, username, first_cs, last_cs):
        """
        Get all changesets for a user within a changeset ID range.
        Since the API limits to 100 per request, we paginate.
        """
        since = f"{MIN_YEAR}-01-01T00:00:00"
        all_ids = []
        # The API returns up to 100 changesets; paginate using the time cursor
        until = datetime.now(tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
        while True:
            xml = self.get_user_changesets(username, since, until)
            ids = re.findall(r'<changeset\s+id="(\d+)"', xml)
            if not ids:
                break
            ids = [int(x) for x in ids]
            all_ids.extend(ids)
            # Use the oldest changeset's time as the new end point for pagination
            timestamps = re.findall(r'<changeset[^>]*created_at="([^"]*)"', xml)
            if timestamps:
                until = timestamps[-1]  # oldest in this batch
            else:
                break

        # Filter to requested range
        filtered = [x for x in all_ids if (first_cs <= x <= last_cs)]
        return filtered

    def get_changeset_info(self, cs_id):
        """Get detailed changeset info including user."""
        xml = self.get_changeset(cs_id)
        if xml is None:
            return None
        m = re.search(r'user="([^"]*)"', xml)
        if m:
            return {'user': m.group(1), 'id': cs_id}
        return None

    def get_changeset_timestamp(self, cs_id):
        """Get the created_at timestamp of a changeset."""
        xml = self.get_changeset(cs_id)
        if xml is None:
            return None
        m = re.search(r'created_at="([^"]*)"', xml)
        if m:
            return m.group(1)
        return None

    def download_changeset(self, cs_id, workdir):
        """Download a single changeset's diff to workdir."""
        outfile = os.path.join(workdir, f"c{cs_id}.osc")
        if os.path.exists(outfile):
            return True
        r = self.session.get(f"{OGF_API_BASE}changeset/{cs_id}/download")
        r.raise_for_status()
        with open(outfile, 'wb') as f:
            f.write(r.content)
        return True


# ── Validation ─────────────────────────────────────────────────────────────────

def validate_request(req, ogf):
    """Validate a revert request against guard rails. Returns (ok, message)."""
    mapper = req['mapper']
    first_cs = req['first_cs']
    last_cs = req['last_cs']

    # If neither specified, we revert all changesets
    if first_cs is None and last_cs is None:
        return True, "Reverting all changesets (will determine range)"

    # Check changeset ownership (case-insensitive comparison)
    if first_cs is not None:
        info = ogf.get_changeset_info(first_cs)
        if info is None:
            return False, f"Changeset {first_cs} not found"
        if info['user'].lower() != mapper.lower():
            return False, f"Changeset {first_cs} belongs to '{info['user']}', not '{mapper}'"
        # Guard rail: check changeset is not before MIN_YEAR
        ts = ogf.get_changeset_timestamp(first_cs)
        if ts and int(ts[:4]) < MIN_YEAR:
            return False, f"Changeset {first_cs} is from {ts[:4]}, before {MIN_YEAR}"

    if last_cs is not None:
        info = ogf.get_changeset_info(last_cs)
        if info is None:
            return False, f"Changeset {last_cs} not found"
        if info['user'].lower() != mapper.lower():
            return False, f"Changeset {last_cs} belongs to '{info['user']}', not '{mapper}'"
        # Guard rail: check changeset is not before MIN_YEAR
        ts = ogf.get_changeset_timestamp(last_cs)
        if ts and int(ts[:4]) < MIN_YEAR:
            return False, f"Changeset {last_cs} is from {ts[:4]}, before {MIN_YEAR}"

    # Get all user changesets for range determination
    all_ids = ogf.get_user_changesets_by_id_range(mapper, first_cs or 0, last_cs or 99999999)

    # Apply range filters based on what's specified
    if first_cs is not None and last_cs is not None:
        # Specific range
        cs_ids = [x for x in all_ids if first_cs <= x <= last_cs]
    elif first_cs is not None:
        # From first_cs onwards (to most recent)
        cs_ids = [x for x in all_ids if x >= first_cs]
    elif last_cs is not None:
        # From start to last_cs
        cs_ids = [x for x in all_ids if x <= last_cs]
    else:
        cs_ids = all_ids

    if len(cs_ids) > MAX_CHANGESETS:
        return False, f"Too many changesets: {len(cs_ids)} (max {MAX_CHANGESETS})"
    if len(cs_ids) == 0:
        return False, "No changesets found in the specified range"

    req['_cs_ids'] = cs_ids
    return True, f"Will revert {len(cs_ids)} changesets"


# ── Revert Execution ──────────────────────────────────────────────────────────

def execute_revert(req, ogf, prefs):
    """
    Execute the two-stage revert: download changesets, then run complex_revert.pl.
    Returns (success, message, revert_changeset_ids).
    """
    mapper = req['mapper']
    first_cs = req['first_cs']
    last_cs = req['last_cs']

    # Create a temporary working directory
    workdir = tempfile.mkdtemp(prefix='ogf_revert_')
    print(f"  Working directory: {workdir}")

    try:
        cs_ids = req.get('_cs_ids', [])

        if not cs_ids:
            return False, "No changeset IDs determined", []

        print(f"  Changesets to revert: {len(cs_ids)} (IDs: {min(cs_ids)}-{max(cs_ids)})")

        # Download all changesets
        for cs_id in cs_ids:
            print(f"  Downloading changeset {cs_id}...")
            ogf.download_changeset(cs_id, workdir)

        # Stage 2: Run complex_revert.pl
        comment = f"Reverting changesets {min(cs_ids)}-{max(cs_ids)} by {mapper} per revert queue request"
        print(f"  Running complex_revert.pl...")

        env = os.environ.copy()

        # Build the cat command for all changeset files
        osc_files = [f"c{cs_id}.osc" for cs_id in sorted(cs_ids)]
        cat_cmd = "cat " + " ".join(osc_files)

        revert_cmd = f"{cat_cmd} | perl {os.path.join(REVERT_SCRIPTS, 'complex_revert.pl')} --no-progress '{comment}'"

        result = subprocess.run(
            revert_cmd,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=3600,
            env=env
        )

        if result.returncode != 0:
            return False, f"Revert failed: {result.stderr}\n{result.stdout}", []

        output = result.stdout
        if output:
            print(f"  Revert output: {output[:500]}")

        # Extract revert changeset IDs from the log file
        revert_cs_ids = []
        log_file = os.path.join(workdir, 'complex_revert.log')
        if os.path.exists(log_file):
            with open(log_file) as f:
                for line in f:
                    m = re.search(r'changeset\s+(\d+)\s+created', line)
                    if m:
                        revert_cs_ids.append(int(m.group(1)))

        return True, f"Successfully reverted {len(cs_ids)} changesets", revert_cs_ids

    finally:
        # Clean up working directory
        shutil.rmtree(workdir, ignore_errors=True)


# ── Wiki Update ────────────────────────────────────────────────────────────────

def update_wiki_entry(content, original_text, mapper, first_cs, last_cs,
                      success, message, requester, revert_cs_ids=None):
    """
    Replace a {{revert-please|...}} entry with {{revert-complete|...}}.
    Template format: {{revert-complete|user|first|last|requester_sig|status|actioner_sig|comment}}
    Returns the updated content.
    """
    status = "success" if success else "fail"

    # Build comment: include message and revert changeset IDs if available
    comment_parts = []
    if message:
        comment_parts.append(message)
    if success and revert_cs_ids:
        comment_parts.append(f"Revert changeset(s): {', '.join(str(x) for x in revert_cs_ids)}")
    comment = '; '.join(comment_parts) if comment_parts else ''

    # ~~~~ will expand to the bot user's signature when the wiki renders it
    replacement = (
        f"{{{{revert-complete"
        f"|{mapper}"
        f"|{first_cs or ''}"
        f"|{last_cs or ''}"
        f"|{requester}"
        f"|{status}"
        f"|~~~~"
        f"|{comment}"
        f"}}}}"
    )

    return content.replace(original_text, replacement, 1)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    dry_run = '--dry-run' in sys.argv
    if dry_run:
        print("DRY RUN MODE - no changes will be made")

    print("=" * 60)
    print("OGF Revert Queue Processor")
    print("=" * 60)

    # Load credentials
    prefs = load_osmtoolsrc()
    wiki_username = prefs.get('username', 'Brothie')
    wiki_password = prefs.get('password', '')

    if not wiki_password:
        print("ERROR: No password found in .osmtoolsrc")
        sys.exit(1)

    # Initialize clients
    wiki = WikiClient(wiki_username, wiki_password)
    ogf = OGFClient(wiki_username, wiki_password)

    # Login to wiki
    print("\nLogging in to wiki...")
    if not wiki.login():
        print("ERROR: Wiki login failed. Cannot proceed.")
        sys.exit(1)

    # Fetch the queue
    print(f"\nFetching revert queue from wiki...")
    content = wiki.get_raw_page(QUEUE_PAGE)
    if content is None:
        print("ERROR: Could not fetch queue page")
        sys.exit(1)

    # Parse requests
    requests = parse_queue(content)
    if not requests:
        print("No pending revert requests found.")
        return

    print(f"Found {len(requests)} pending request(s)\n")

    # Permission gate
    if not check_bot_permission():
        print("Aborting - bot permission not granted.")
        sys.exit(0)

    print()

    modified = False
    new_content = content

    for i, req in enumerate(requests, 1):
        print(f"--- Request {i}: Revert '{req['mapper']}' "
              f"(changesets {req['first_cs'] or 'all'} to {req['last_cs'] or 'all'}) "
              f"requested by {req['requester']} ---")

        # Validate
        print("  Validating...")
        ok, msg = validate_request(req, ogf)
        if not ok:
            print(f"  FAILED: {msg}")
            if not dry_run:
                new_content = update_wiki_entry(
                    new_content, req['original_text'],
                    req['mapper'], req['first_cs'], req['last_cs'],
                    False, msg, req['requester']
                )
            modified = True
            continue

        print(f"  Validation passed: {msg}")

        # Execute revert
        if dry_run:
            cs_ids = req.get('_cs_ids', [])
            print(f"  DRY RUN: Would revert {len(cs_ids) if cs_ids else 'unknown'} changesets")
            success, msg, revert_cs_ids = True, "Dry run - no changes made", []
        else:
            print("  Executing revert...")
            success, msg, revert_cs_ids = execute_revert(req, ogf, prefs)

        if success:
            print(f"  SUCCESS: {msg}")
        else:
            print(f"  FAILED: {msg}")

        # Update wiki
        if dry_run:
            # In dry run, still do the replacement to show preview
            new_content = update_wiki_entry(
                new_content, req['original_text'],
                req['mapper'], req['first_cs'], req['last_cs'],
                success, msg, req['requester'],
                revert_cs_ids if success else None
            )
            modified = True
        else:
            new_content = update_wiki_entry(
                new_content, req['original_text'],
                req['mapper'], req['first_cs'], req['last_cs'],
                success, msg, req['requester'],
                revert_cs_ids if success else None
            )
            modified = True

    # Save changes to wiki
    if modified:
        if dry_run:
            print(f"\nDRY RUN: Would save updated queue to wiki")
            print("\nPreview of changed entries:")
            for line in new_content.split('\n'):
                line = line.strip()
                if line.startswith('* {{revert-'):
                    print(f"  {line}")
        else:
            print(f"\nSaving updated queue to wiki...")
            result = wiki.edit_page(
                QUEUE_PAGE,
                new_content,
                "Automated revert queue processing"
            )
            print(f"  Wiki updated. New revision: {result.get('newrevid', 'unknown')}")
    else:
        print("\nNo changes to save.")

    print("\nDone.")


if __name__ == '__main__':
    main()
