import httpx
import asyncio
import typer
from tqdm.asyncio import tqdm
from tabulate import tabulate
from dotenv import load_dotenv
import os
from typing import List

# Load .env
load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
if not GITHUB_TOKEN:
    raise ValueError("GITHUB_TOKEN is missing! Please add it to your .env file.")

GITHUB_API = "https://api.github.com/graphql"

app = typer.Typer()

PR_COUNT_QUERY = """
query GetPrCount($owner: String!, $repo: String!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequests(first: 100, after: $after, states: MERGED) {
      totalCount
      pageInfo {
        endCursor
        hasNextPage
      }
      edges {
        node {
          number
        }
      }
    }
  }
}
"""

async def retry_request(client: httpx.AsyncClient, url: str, method: str = "get", **kwargs):
    """Retry a request with exponential backoff."""
    max_retries = 5
    retry_delay = 1
    
    for attempt in range(max_retries):
        try:
            if method.lower() == "get":
                response = await client.get(url, **kwargs)
            else:  # post
                response = await client.post(url, **kwargs)
                
            # Check for rate limiting
            if response.status_code == 403 and "rate limit" in response.text.lower():
                retry_after = int(response.headers.get("Retry-After", retry_delay))
                print(f"⚠️ Rate limited. Waiting {retry_after} seconds...")
                await asyncio.sleep(retry_after)
                continue
                
            # Check for other errors
            if response.status_code >= 400:
                print(f"⚠️ Request failed with status {response.status_code}. Retrying in {retry_delay} seconds...")
                await asyncio.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
                continue
                
            return response
            
        except (httpx.RequestError, httpx.TimeoutException) as e:
            if attempt < max_retries - 1:
                print(f"⚠️ Request error: {e}. Retrying in {retry_delay} seconds...")
                await asyncio.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
            else:
                raise
    
    raise Exception(f"Failed after {max_retries} retries")

async def fetch_pr_count(client: httpx.AsyncClient, owner: str, repo: str):
    """Fetch the total count of merged PRs for a repository."""
    after = None
    pr_count = 0
    
    # Get initial response to get total count
    initial_response = await retry_request(
        client,
        GITHUB_API,
        "post",
        json={"query": PR_COUNT_QUERY, "variables": {"owner": owner, "repo": repo, "after": after}},
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}"},
    )
    
    initial_data = initial_response.json()
    
    if "errors" in initial_data:
        print(f"❌ Error fetching PR count: {initial_data['errors']}")
        return 0
    
    total_count = initial_data.get("data", {}).get("repository", {}).get("pullRequests", {}).get("totalCount", 0)
    
    with tqdm(desc=f"Counting PRs for {owner}/{repo}", unit="batch", total=total_count) as pbar:
        pull_requests = initial_data.get("data", {}).get("repository", {}).get("pullRequests", {})
        edges = pull_requests.get("edges", [])
        page_info = pull_requests.get("pageInfo", {})
        
        pr_count += len(edges)
        after = page_info.get("endCursor")
        pbar.update(len(edges))
        
        # Continue with pagination
        while page_info.get("hasNextPage"):
            response = await retry_request(
                client,
                GITHUB_API,
                "post",
                json={"query": PR_COUNT_QUERY, "variables": {"owner": owner, "repo": repo, "after": after}},
                headers={"Authorization": f"Bearer {GITHUB_TOKEN}"},
            )
            
            data = response.json()
            
            # Check for errors in the GraphQL response
            if "errors" in data:
                print(f"❌ Error fetching PR count: {data['errors']}")
                break
                
            pull_requests = data.get("data", {}).get("repository", {}).get("pullRequests", {})
            edges = pull_requests.get("edges", [])
            page_info = pull_requests.get("pageInfo", {})
            
            pr_count += len(edges)
            after = page_info.get("endCursor")
            pbar.update(len(edges))
    
    return pr_count

def parse_repo_string(repo_string: str):
    """Parse a repository string in the format 'owner/repo'."""
    parts = repo_string.strip().split('/')
    if len(parts) != 2:
        raise ValueError(f"Invalid repository format: {repo_string}. Expected format: owner/repo")
    return parts[0], parts[1]

@app.command()
def count_prs(
    repos: List[str] = typer.Argument(..., help="Repository or repositories in the format 'owner/repo' or multiple 'owner/repo' entries")
):
    """Count the total number of merged PRs for one or more repositories."""
    async def main():
        async with httpx.AsyncClient() as client:
            table_data = []
            
            # Clean up repository strings to handle comma-separated input
            clean_repos = []
            for repo_arg in repos:
                # Split by comma and process each repository
                for repo in repo_arg.split(','):
                    repo = repo.strip()
                    if repo:  # Skip empty strings
                        clean_repos.append(repo)
            
            for repo_string in clean_repos:
                try:
                    owner, repo = parse_repo_string(repo_string)
                    pr_count = await fetch_pr_count(client, owner, repo)
                    
                    # Print the repository information
                    print(f"\n📂 Repository: {owner}/{repo}")
                    print(f"🔄 Total Merged PRs: {pr_count}")
                    
                    table_data.append([owner, repo, pr_count])
                except ValueError as e:
                    print(f"\n❌ Error: {e}")
            
            # Display all repositories in a table format
            if table_data:
                print("\n" + tabulate(table_data, headers=["Owner", "Repository", "Merged PRs"], tablefmt="fancy_grid"))
            else:
                print("\n❌ No valid repositories were processed.")
    
    asyncio.run(main())

if __name__ == "__main__":
    app()
