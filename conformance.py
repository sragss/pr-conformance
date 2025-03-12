import httpx
import asyncio
import typer
from tqdm.asyncio import tqdm
from typing import Dict, Set
from tabulate import tabulate
from dotenv import load_dotenv
import os
import time
import matplotlib.pyplot as plt
from datetime import datetime
from collections import defaultdict

# Load .env
load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
if not GITHUB_TOKEN:
    raise ValueError("GITHUB_TOKEN is missing! Please add it to your .env file.")

GITHUB_API = "https://api.github.com/graphql"

app = typer.Typer()

BRANCH_COMMITS_QUERY = """
query GetCommits($owner: String!, $repo: String!, $branch: String!, $after: String) {
  repository(owner: $owner, name: $repo) {
    ref(qualifiedName: $branch) {
      target {
        ... on Commit {
          history(first: 100, after: $after) {
            totalCount
            pageInfo {
              endCursor
              hasNextPage
            }
            edges {
              node {
                oid
                committedDate
                author {
                  user {
                    login
                    url
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

PR_COMMITS_QUERY = """
query GetPrCommits($owner: String!, $repo: String!, $after: String) {
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
          mergeCommit {
            oid
          }
          commits(first: 100) {
            edges {
              node {
                commit {
                  oid
                }
              }
            }
          }
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

async def fetch_commits(client: httpx.AsyncClient, owner: str, repo: str, branch: str, query_counts: Dict[str, int]):
    """Fetch all commit SHAs, dates, authors, and default branch."""
    commits = {}
    after = None
    
    # First, get the default branch using REST API
    default_branch_response = await retry_request(
        client,
        f"https://api.github.com/repos/{owner}/{repo}",
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}"}
    )
    query_counts["REST API"] = query_counts.get("REST API", 0) + 1
    
    repo_data = default_branch_response.json()
    default_branch = repo_data.get("default_branch", branch)  # Fallback to user-provided branch if not found
    
    # Get initial response to get total count
    initial_response = await retry_request(
        client,
        GITHUB_API,
        "post",
        json={"query": BRANCH_COMMITS_QUERY, "variables": {"owner": owner, "repo": repo, "branch": default_branch, "after": after}},
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}"},
    )
    query_counts["GraphQL"] = query_counts.get("GraphQL", 0) + 1
    
    initial_data = initial_response.json()
    
    total_count = 0
    if "data" in initial_data and initial_data["data"].get("repository", {}).get("ref"):
        total_count = initial_data["data"]["repository"]["ref"]["target"]["history"].get("totalCount", 0)
    
    with tqdm(desc="Fetching Commits", unit="batch", total=total_count) as pbar:
        # Process the initial response
        if "errors" in initial_data:
            print(f"❌ Error fetching commits: {initial_data['errors']}")
            return {}, default_branch

        repository = initial_data.get("data", {}).get("repository", {})

        # Handle case where `ref` is None (missing branch)
        if not repository.get("ref"):
            print(f"⚠️ Warning: No branch '{branch}' found in {owner}/{repo}. The repository may be empty or private.")
            return {}, default_branch  # Return empty commit list

        history = repository["ref"].get("target", {}).get("history", {})
        edges = history.get("edges", [])
        page_info = history.get("pageInfo", {})

        for edge in edges:
            commit_node = edge["node"]
            author_login = commit_node["author"]["user"]["login"] if commit_node["author"] and commit_node["author"]["user"] else "Unknown"
            author_url = commit_node["author"]["user"]["url"] if commit_node["author"] and commit_node["author"]["user"] else "N/A"

            commits[commit_node["oid"]] = {
                "date": commit_node["committedDate"],
                "author": author_login,
                "author_url": author_url,
            }

        after = page_info.get("endCursor")
        pbar.update(len(edges))

        # Continue with pagination
        while page_info.get("hasNextPage"):
            response = await retry_request(
                client,
                GITHUB_API,
                "post",
                json={"query": BRANCH_COMMITS_QUERY, "variables": {"owner": owner, "repo": repo, "branch": default_branch, "after": after}},
                headers={"Authorization": f"Bearer {GITHUB_TOKEN}"},
            )
            query_counts["GraphQL"] = query_counts.get("GraphQL", 0) + 1
            
            data = response.json()

            # Check for errors in the GraphQL response
            if "errors" in data:
                print(f"❌ Error fetching commits: {data['errors']}")
                break

            repository = data.get("data", {}).get("repository", {})
            if not repository.get("ref"):
                break

            history = repository["ref"].get("target", {}).get("history", {})
            edges = history.get("edges", [])
            page_info = history.get("pageInfo", {})

            for edge in edges:
                commit_node = edge["node"]
                author_login = commit_node["author"]["user"]["login"] if commit_node["author"] and commit_node["author"]["user"] else "Unknown"
                author_url = commit_node["author"]["user"]["url"] if commit_node["author"] and commit_node["author"]["user"] else "N/A"

                commits[commit_node["oid"]] = {
                    "date": commit_node["committedDate"],
                    "author": author_login,
                    "author_url": author_url,
                }

            after = page_info.get("endCursor")
            pbar.update(len(edges))

    return commits, default_branch

async def fetch_pr_commits(client: httpx.AsyncClient, owner: str, repo: str, query_counts: Dict[str, int]) -> Set[str]:
    """Fetch all commit SHAs that have been merged through PRs."""
    pr_commits = set()
    after = None
    pr_count = 0
    
    # Get initial response to get total count
    initial_response = await retry_request(
        client,
        GITHUB_API,
        "post",
        json={"query": PR_COMMITS_QUERY, "variables": {"owner": owner, "repo": repo, "after": after}},
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}"},
    )
    query_counts["GraphQL"] = query_counts.get("GraphQL", 0) + 1
    
    initial_data = initial_response.json()
    
    total_count = 0
    if "data" in initial_data and initial_data["data"].get("repository", {}):
        total_count = initial_data["data"]["repository"]["pullRequests"].get("totalCount", 0)

    with tqdm(desc="Fetching PR Commits", unit="batch", total=total_count) as pbar:
        # Process the initial response
        if "errors" in initial_data:
            print(f"❌ Error fetching PR commits: {initial_data['errors']}")
            return set(), 0
            
        pull_requests = initial_data.get("data", {}).get("repository", {}).get("pullRequests", {})
        edges = pull_requests.get("edges", [])
        page_info = pull_requests.get("pageInfo", {})

        for pr in edges:
            pr_node = pr["node"]
            pr_count += 1
            if pr_node["mergeCommit"]:
                pr_commits.add(pr_node["mergeCommit"]["oid"])
            for commit in pr_node["commits"]["edges"]:
                pr_commits.add(commit["node"]["commit"]["oid"])

        after = page_info.get("endCursor")
        pbar.update(len(edges))
        
        # Continue with pagination
        while page_info.get("hasNextPage"):
            response = await retry_request(
                client,
                GITHUB_API,
                "post",
                json={"query": PR_COMMITS_QUERY, "variables": {"owner": owner, "repo": repo, "after": after}},
                headers={"Authorization": f"Bearer {GITHUB_TOKEN}"},
            )
            query_counts["GraphQL"] = query_counts.get("GraphQL", 0) + 1
            
            data = response.json()
            
            # Check for errors in the GraphQL response
            if "errors" in data:
                print(f"❌ Error fetching PR commits: {data['errors']}")
                break
                
            pull_requests = data.get("data", {}).get("repository", {}).get("pullRequests", {})
            edges = pull_requests.get("edges", [])
            page_info = pull_requests.get("pageInfo", {})

            for pr in edges:
                pr_node = pr["node"]
                pr_count += 1
                if pr_node["mergeCommit"]:
                    pr_commits.add(pr_node["mergeCommit"]["oid"])
                for commit in pr_node["commits"]["edges"]:
                    pr_commits.add(commit["node"]["commit"]["oid"])

            after = page_info.get("endCursor")
            pbar.update(len(edges))

    return pr_commits, pr_count

def plot_conformance_over_time(default_branch_commits, pr_commits, owner, repo, default_branch):
    """Plot PR conformance over time."""
    # Convert dates to datetime objects and sort commits by date
    dated_commits = []
    for sha, data in default_branch_commits.items():
        date = datetime.fromisoformat(data["date"].replace("Z", "+00:00"))
        is_pr = sha in pr_commits
        dated_commits.append((date, is_pr))
    
    # Sort commits by date
    dated_commits.sort()
    
    if not dated_commits:
        print("⚠️ No commits to analyze for time trend.")
        return
    
    # Group by month for a more readable chart
    monthly_data = defaultdict(lambda: {"total": 0, "pr": 0})
    
    for date, is_pr in dated_commits:
        month_key = date.strftime("%Y-%m")
        monthly_data[month_key]["total"] += 1
        if is_pr:
            monthly_data[month_key]["pr"] += 1
    
    # Calculate conformance rate by month
    months = sorted(monthly_data.keys())
    conformance_rates = []
    
    for month in months:
        total = monthly_data[month]["total"]
        pr = monthly_data[month]["pr"]
        rate = pr / total if total > 0 else 1.0
        conformance_rates.append(rate)
    
    # Create the plot
    plt.figure(figsize=(12, 6))
    plt.plot(months, conformance_rates, marker='o', linestyle='-', color='blue')
    plt.title(f"PR Conformance Rate Over Time for {owner}/{repo} ({default_branch})")
    plt.xlabel("Month")
    plt.ylabel("Conformance Rate")
    plt.ylim(0, 1.1)  # Set y-axis from 0 to 1.1 to give some space above 100%
    plt.grid(True, linestyle='--', alpha=0.7)
    plt.xticks(rotation=45)
    
    # Add percentage labels on the y-axis
    plt.yticks([i/10 for i in range(0, 11)], [f"{i*10}%" for i in range(0, 11)])
    
    # Tight layout to ensure everything fits
    plt.tight_layout()
    
    # Save the plot
    filename = f"{owner}_{repo}_conformance_trend.png"
    plt.savefig(filename)
    print(f"📈 Conformance trend chart saved as {filename}")
    
    # Show the plot
    plt.show()


@app.command()
def analyze_commits(owner: str, repo: str, branch: str = "main", show_commits: bool = False, show_trend: bool = True):
    """Check which commits in the default branch were not merged via a PR."""
    async def main():
        query_counts = {"GraphQL": 0, "REST API": 0}
        
        async with httpx.AsyncClient() as client:
            default_branch_commits, default_branch = await fetch_commits(client, owner, repo, branch, query_counts)
            pr_commits, pr_count = await fetch_pr_commits(client, owner, repo, query_counts)

            not_via_pr = {sha: data for sha, data in default_branch_commits.items() if sha not in pr_commits}

            # Print the repository information
            print(f"\n📂 Repository: {owner}/{repo}")
            print(f"🟢 Default Branch: {default_branch}")
            print(f"📌 Commits in {default_branch}: {len(default_branch_commits)}")
            print(f"🔄 Merged PRs: {pr_count}")
            print(f"✅ Commits in merged PRs: {len(pr_commits)}")
            print(f"❌ Commits Not via PR: {len(not_via_pr)}")
            conformance_rate = 1 - (len(not_via_pr) / len(default_branch_commits)) if default_branch_commits else 1.0
            print(f"📊 Conformance Rate: {conformance_rate:.2%}")

            if not_via_pr and show_commits:
                print("\n🛑 Commits Not via PR:")
                table_data = [
                    [
                        data["date"],
                        data["author"],
                        data["author_url"],
                        f"https://github.com/{owner}/{repo}/commit/{sha}",
                    ]
                    for sha, data in not_via_pr.items()
                ]
                print(tabulate(table_data, headers=["Date", "Author", "Author Profile", "Commit Link"], tablefmt="fancy_grid"))
            
            if show_trend:
                plot_conformance_over_time(default_branch_commits, pr_commits, owner, repo, default_branch)
            
            print("\n📊 API Query Statistics:")
            for endpoint, count in query_counts.items():
                print(f"  {endpoint}: {count} queries")

    asyncio.run(main())

if __name__ == "__main__":
    app()
