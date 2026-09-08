"""
Jira API Client — Core Library
Reusable module for accessing Jira Cloud REST API v3.
Auth via ATLASSIAN_API_TOKEN env var + HTTPBasicAuth.

Usage as library:
    from jira_client import search_issues, get_issue, get_issue_comments

Usage as CLI:
    python jira_client.py test              Test authentication + fetch sample issue
    python jira_client.py search "JQL"      Quick search
    python jira_client.py issue PIP-123     Show issue details
    python jira_client.py count "JQL"       Count matching issues
"""

import sys
import os
import json
import time
import requests
from requests.auth import HTTPBasicAuth

# Fix Windows console encoding
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ── Configuration ──────────────────────────────────────────────────────────
ATLASSIAN_DOMAIN = "edgeaisolutions.atlassian.net"
EMAIL = "edge.ai.solutions@gmail.com"
BASE_URL = f"https://{ATLASSIAN_DOMAIN}"
JIRA_API = f"{BASE_URL}/rest/api/3"
AGILE_API = f"{BASE_URL}/rest/agile/1.0"

DEFAULT_TIMEOUT = 15
DEFAULT_DELAY = 0.1  # seconds between API calls to avoid rate limiting


class JiraAPIError(Exception):
    """Raised when a Jira API call fails."""
    def __init__(self, message, status_code=0):
        super().__init__(message)
        self.status_code = status_code


# ── Authentication ─────────────────────────────────────────────────────────

def get_auth():
    """Get HTTPBasicAuth from environment variable."""
    token = os.environ.get("ATLASSIAN_API_TOKEN", "")
    if not token:
        print("ERROR: No API token. Set ATLASSIAN_API_TOKEN env var.")
        print("  set ATLASSIAN_API_TOKEN=your_token_here")
        sys.exit(1)
    return HTTPBasicAuth(EMAIL, token)


# ── HTTP Helpers ───────────────────────────────────────────────────────────

def _request(method, url, auth, params=None, json_body=None, timeout=DEFAULT_TIMEOUT):
    """HTTP request with error handling. Returns (json_data, status_code)."""
    try:
        resp = requests.request(
            method, url, auth=auth, params=params, json=json_body, timeout=timeout
        )
        if resp.ok:
            if resp.status_code == 204 or not resp.content:
                return {}, resp.status_code
            return resp.json(), resp.status_code
        else:
            return {"error": resp.text[:1000]}, resp.status_code
    except requests.exceptions.Timeout:
        return {"error": f"Timeout after {timeout}s"}, 0
    except Exception as e:
        return {"error": str(e)}, 0


def _get(url, auth, params=None, timeout=DEFAULT_TIMEOUT):
    """GET shorthand."""
    return _request("GET", url, auth, params=params, timeout=timeout)


def _put(url, auth, json_body, timeout=DEFAULT_TIMEOUT):
    """PUT shorthand."""
    return _request("PUT", url, auth, json_body=json_body, timeout=timeout)


def _post(url, auth, json_body=None, timeout=DEFAULT_TIMEOUT):
    """POST shorthand."""
    return _request("POST", url, auth, json_body=json_body, timeout=timeout)


# ── Issue Create / Update ─────────────────────────────────────────────────

def create_issue(project_key, issue_type, summary, description_adf=None,
                 labels=None, priority=None, extra_fields=None):
    """Create a new Jira issue.

    Args:
        project_key: e.g. "PIP"
        issue_type: e.g. "Task"
        summary: issue title
        description_adf: ADF document dict (optional)
        labels: list of label strings (optional)
        priority: e.g. "Medium" (optional)
        extra_fields: dict of additional fields to set (optional)

    Returns:
        dict with at least 'id', 'key', 'self' fields
    """
    auth = get_auth()
    fields = {
        "project": {"key": project_key},
        "issuetype": {"name": issue_type},
        "summary": summary,
    }
    if description_adf:
        fields["description"] = description_adf
    if labels:
        fields["labels"] = labels
    if priority:
        fields["priority"] = {"name": priority}
    if extra_fields:
        fields.update(extra_fields)

    data, status = _post(f"{JIRA_API}/issue", auth, json_body={"fields": fields})
    if status not in (200, 201):
        raise JiraAPIError(
            f"Create failed ({status}): {data.get('error', data)}", status
        )
    return data


def add_labels(issue_key, labels):
    """Add labels to an existing issue (without removing existing ones).

    Args:
        issue_key: e.g. "PIP-1234"
        labels: list of label strings to add
    """
    auth = get_auth()
    payload = {"update": {"labels": [{"add": lbl} for lbl in labels]}}
    data, status = _put(f"{JIRA_API}/issue/{issue_key}", auth, json_body=payload)
    if status not in (200, 204):
        raise JiraAPIError(
            f"Label update failed ({status}): {data.get('error', data)}", status
        )
    return data


def add_comment(issue_key, body_adf):
    """Add a comment to an issue.

    Args:
        issue_key: e.g. "PIP-1234"
        body_adf: ADF document dict for the comment body
    """
    auth = get_auth()
    data, status = _post(
        f"{JIRA_API}/issue/{issue_key}/comment", auth,
        json_body={"body": body_adf}
    )
    if status not in (200, 201):
        raise JiraAPIError(
            f"Comment failed ({status}): {data.get('error', data)}", status
        )
    return data


def create_link(outward_key, inward_key, link_type_name="Derive"):
    """Create an issue link between two issues.

    For "is derived from" links the return ticket is the outward issue
    and the source (client/equipment) is the inward issue.

    Args:
        outward_key: the return ticket, e.g. "PIP-7801"
        inward_key: the source ticket (client CR or equipment), e.g. "PIP-2136"
        link_type_name: link type name (default "Derive")
    """
    auth = get_auth()
    payload = {
        "type": {"name": link_type_name},
        "outwardIssue": {"key": outward_key},
        "inwardIssue": {"key": inward_key},
    }
    data, status = _post(f"{JIRA_API}/issueLink", auth, json_body=payload)
    if status not in (200, 201):
        raise JiraAPIError(
            f"Link failed ({status}): {data.get('error', data)}", status
        )
    return data


# ── Core Issue Operations ──────────────────────────────────────────────────

def get_issue(issue_key, fields=None, expand=None):
    """Fetch a single issue with all fields.

    Args:
        issue_key: e.g. "PIP-1234"
        fields: comma-separated field list or None for all
        expand: comma-separated expand list (e.g. "changelog,renderedFields")

    Returns:
        dict with issue data, or raises JiraAPIError
    """
    auth = get_auth()
    params = {}
    if fields:
        params["fields"] = fields
    if expand:
        params["expand"] = expand

    data, status = _get(f"{JIRA_API}/issue/{issue_key}", auth, params=params)
    if status != 200:
        raise JiraAPIError(f"Failed to fetch {issue_key} ({status}): {data.get('error', data)}", status)
    return data


def search_issues(jql, fields=None, max_results=50):
    """Search issues with JQL. Returns first page only.

    Args:
        jql: JQL query string
        fields: comma-separated field list
        max_results: max issues to return (max 100)

    Returns:
        list of issue dicts
    """
    auth = get_auth()
    params = {"jql": jql, "maxResults": min(max_results, 100)}
    if fields:
        params["fields"] = fields

    data, status = _get(f"{JIRA_API}/search/jql", auth, params=params)
    if status != 200:
        raise JiraAPIError(f"Search failed ({status}): {data.get('error', data)}", status)
    return data.get("issues", [])


def search_issues_paginated(jql, fields=None, max_per_page=100, delay=DEFAULT_DELAY,
                             progress_fn=None):
    """Fetch ALL matching issues with cursor-based pagination.

    Uses nextPageToken (NOT offset-based startAt).

    Args:
        jql: JQL query string
        fields: comma-separated field list or None for all
        max_per_page: results per page (max 100)
        delay: seconds between API calls
        progress_fn: callback(fetched_count, total_estimate, page_num)

    Returns:
        list of all matching issue dicts
    """
    auth = get_auth()
    all_issues = []
    params = {"jql": jql, "maxResults": min(max_per_page, 100)}
    if fields:
        params["fields"] = fields

    page = 0
    while True:
        data, status = _get(f"{JIRA_API}/search/jql", auth, params=params)
        if status != 200:
            raise JiraAPIError(f"Search failed ({status}): {data.get('error', data)}", status)

        issues = data.get("issues", [])
        all_issues.extend(issues)
        page += 1

        is_last = data.get("isLast", True)
        if progress_fn:
            progress_fn(len(all_issues), "?" if not is_last else len(all_issues), page)

        # Cursor-based pagination
        next_token = data.get("nextPageToken")
        if not next_token or data.get("isLast", True):
            break
        params["nextPageToken"] = next_token

        if delay > 0:
            time.sleep(delay)

    return all_issues


def count_issues(jql):
    """Count issues matching JQL by paginating with keys only.

    Note: The /search/jql endpoint does NOT return a 'total' field.
    We paginate through all results fetching only keys.

    Returns:
        int count
    """
    auth = get_auth()
    count = 0
    params = {"jql": jql, "maxResults": 100, "fields": "key"}

    while True:
        data, status = _get(f"{JIRA_API}/search/jql", auth, params=params)
        if status != 200:
            raise JiraAPIError(f"Count failed ({status}): {data.get('error', data)}", status)

        count += len(data.get("issues", []))

        next_token = data.get("nextPageToken")
        if not next_token or data.get("isLast", True):
            break
        params["nextPageToken"] = next_token

    return count


def get_issue_comments(issue_key, max_results=100):
    """Fetch all comments for an issue.

    Args:
        issue_key: e.g. "PIP-1234"
        max_results: max comments per page

    Returns:
        list of comment dicts with keys: id, author, created, updated, body (ADF)
    """
    auth = get_auth()
    all_comments = []
    start_at = 0

    while True:
        params = {"startAt": start_at, "maxResults": max_results}
        data, status = _get(f"{JIRA_API}/issue/{issue_key}/comment", auth, params=params)
        if status != 200:
            raise JiraAPIError(
                f"Comments failed for {issue_key} ({status}): {data.get('error', data)}", status
            )

        comments = data.get("comments", [])
        for c in comments:
            all_comments.append({
                "id": c.get("id"),
                "author": (c.get("author") or {}).get("displayName", "Unknown"),
                "author_id": (c.get("author") or {}).get("accountId", ""),
                "created": c.get("created"),
                "updated": c.get("updated"),
                "body": c.get("body"),  # ADF format
            })

        start_at += len(comments)
        total = data.get("total", 0)
        if start_at >= total or not comments:
            break

    return all_comments


def get_issue_links(issue_data):
    """Extract normalized links from an issue's fields.

    Args:
        issue_data: full issue dict from get_issue() or search result

    Returns:
        list of dicts: {type, direction, key, summary}
    """
    fields = issue_data.get("fields", {})
    raw_links = fields.get("issuelinks", [])
    links = []

    for link in raw_links:
        link_type = link.get("type", {}).get("name", "Unknown")

        if "outwardIssue" in link:
            target = link["outwardIssue"]
            links.append({
                "type": link_type,
                "direction": "outward",
                "key": target.get("key", ""),
                "summary": target.get("fields", {}).get("summary", ""),
            })
        if "inwardIssue" in link:
            target = link["inwardIssue"]
            links.append({
                "type": link_type,
                "direction": "inward",
                "key": target.get("key", ""),
                "summary": target.get("fields", {}).get("summary", ""),
            })

    return links


# ── Metadata ───────────────────────────────────────────────────────────────

def whoami():
    """Test authentication and return current user info."""
    auth = get_auth()
    data, status = _get(f"{JIRA_API}/myself", auth)
    if status != 200:
        raise JiraAPIError(f"Auth failed ({status}): {data.get('error', data)}", status)
    return data


def get_project_labels(project_key="PIP"):
    """Get all labels used in a project by searching."""
    # Jira doesn't have a direct "list labels" endpoint,
    # but we can extract from a search result
    auth = get_auth()
    params = {
        "jql": f"project = {project_key} AND labels IS NOT EMPTY",
        "maxResults": 100,
        "fields": "labels",
    }
    data, status = _get(f"{JIRA_API}/search/jql", auth, params=params)
    if status != 200:
        return []

    labels = set()
    for issue in data.get("issues", []):
        for label in issue.get("fields", {}).get("labels", []):
            labels.add(label)
    return sorted(labels)


# ── CLI ────────────────────────────────────────────────────────────────────

def _print_issue_summary(issue):
    """Print a one-line issue summary."""
    f = issue.get("fields", {})
    key = issue.get("key", "?")
    itype = f.get("issuetype", {}).get("name", "")
    status = f.get("status", {}).get("name", "")
    summary = (f.get("summary") or "")[:60]
    labels = ", ".join(f.get("labels", []))
    print(f"  {key:<12} {itype:<16} {status:<16} {summary}")
    if labels:
        print(f"  {'':12} Labels: {labels}")


def cmd_test():
    """Test authentication and fetch a sample issue."""
    print("Testing Jira API authentication...")
    try:
        user = whoami()
        print(f"  Authenticated as: {user.get('displayName')} ({user.get('emailAddress')})")
        print(f"  Account ID: {user.get('accountId')}")
    except JiraAPIError as e:
        print(f"  FAILED: {e}")
        return False

    print("\nCounting PIP issues from 2025...")
    try:
        count = count_issues('project = PIP AND created >= "2025-01-01"')
        print(f"  Total: {count} issues")
    except JiraAPIError as e:
        print(f"  FAILED: {e}")
        return False

    print("\nFetching 3 recent issues...")
    try:
        issues = search_issues(
            'project = PIP AND created >= "2025-01-01" ORDER BY updated DESC',
            fields="summary,status,issuetype,labels",
            max_results=3,
        )
        for issue in issues:
            _print_issue_summary(issue)
    except JiraAPIError as e:
        print(f"  FAILED: {e}")
        return False

    print("\nAll tests passed.")
    return True


def cmd_search(jql):
    """Search and display results."""
    print(f"Searching: {jql}\n")
    try:
        issues = search_issues(jql, fields="summary,status,issuetype,labels", max_results=20)
        print(f"  {'Key':<12} {'Type':<16} {'Status':<16} Summary")
        print(f"  {'-'*12} {'-'*16} {'-'*16} {'-'*40}")
        for issue in issues:
            _print_issue_summary(issue)
        print(f"\n  Showing {len(issues)} results")
    except JiraAPIError as e:
        print(f"  FAILED: {e}")


def cmd_issue(issue_key):
    """Show full issue details."""
    print(f"Fetching {issue_key}...\n")
    try:
        data = get_issue(issue_key)
        f = data.get("fields", {})
        print(f"  Key         : {data.get('key')}")
        print(f"  Summary     : {f.get('summary')}")
        print(f"  Type        : {f.get('issuetype', {}).get('name')}")
        print(f"  Status      : {f.get('status', {}).get('name')}")
        print(f"  Priority    : {(f.get('priority') or {}).get('name', 'None')}")
        print(f"  Assignee    : {(f.get('assignee') or {}).get('displayName', 'Unassigned')}")
        print(f"  Reporter    : {(f.get('reporter') or {}).get('displayName', 'N/A')}")
        print(f"  Created     : {f.get('created')}")
        print(f"  Updated     : {f.get('updated')}")
        labels = f.get("labels", [])
        if labels:
            print(f"  Labels      : {', '.join(labels)}")

        links = get_issue_links(data)
        if links:
            print(f"  Links       : {len(links)}")
            for link in links:
                print(f"    {link['direction']:8} {link['type']:15} {link['key']} - {link['summary'][:40]}")

        desc = f.get("description")
        if desc:
            # Import adf_utils if available for text conversion
            try:
                from adf_utils import adf_to_text
                print(f"\n  Description:\n{'─'*60}")
                text = adf_to_text(desc)
                for line in text.split('\n'):
                    print(f"    {line}")
            except ImportError:
                print(f"  Description : (ADF format, {len(json.dumps(desc))} chars)")

        comments = get_issue_comments(issue_key)
        if comments:
            print(f"\n  Comments ({len(comments)}):")
            for c in comments[:5]:
                print(f"    [{c['created'][:10]}] {c['author']}: ", end="")
                try:
                    from adf_utils import adf_to_text
                    text = adf_to_text(c['body'])
                    print(text[:100].replace('\n', ' '))
                except ImportError:
                    print("(ADF)")
            if len(comments) > 5:
                print(f"    ... and {len(comments) - 5} more")

    except JiraAPIError as e:
        print(f"  FAILED: {e}")


def cmd_count(jql):
    """Count matching issues."""
    try:
        count = count_issues(jql)
        print(f"  {count} issues match: {jql}")
    except JiraAPIError as e:
        print(f"  FAILED: {e}")


def main():
    if len(sys.argv) < 2:
        print("Jira API Client")
        print("-" * 40)
        print("Commands:")
        print("  test                Test authentication")
        print("  search \"JQL\"        Search issues")
        print("  issue PIP-123       Show issue details")
        print("  count \"JQL\"         Count matching issues")
        return

    cmd = sys.argv[1].lower()

    if cmd == "test":
        cmd_test()
    elif cmd == "search":
        if len(sys.argv) < 3:
            print('Usage: python jira_client.py search "JQL query"')
            return
        cmd_search(" ".join(sys.argv[2:]))
    elif cmd == "issue":
        if len(sys.argv) < 3:
            print("Usage: python jira_client.py issue PIP-123")
            return
        cmd_issue(sys.argv[2])
    elif cmd == "count":
        if len(sys.argv) < 3:
            print('Usage: python jira_client.py count "JQL query"')
            return
        cmd_count(" ".join(sys.argv[2:]))
    else:
        print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
