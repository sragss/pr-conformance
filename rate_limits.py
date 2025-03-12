import httpx
import asyncio
import typer
import os
from dotenv import load_dotenv
from tabulate import tabulate
from datetime import datetime

# Load .env
load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
if not GITHUB_TOKEN:
    raise ValueError("GITHUB_TOKEN is missing! Please add it to your .env file.")

app = typer.Typer()

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

@app.command()
def check_rate_limits():
    """Check the current GitHub API rate limits for your token."""
    async def main():
        async with httpx.AsyncClient() as client:
            # Query REST API rate limits
            rest_response = await retry_request(
                client,
                "https://api.github.com/rate_limit",
                headers={"Authorization": f"Bearer {GITHUB_TOKEN}"}
            )
            
            rest_data = rest_response.json()
            
            # Query GraphQL API rate limits
            graphql_response = await retry_request(
                client,
                "https://api.github.com/graphql",
                method="post",
                json={"query": "query { rateLimit { limit remaining resetAt used } }"},
                headers={"Authorization": f"Bearer {GITHUB_TOKEN}"}
            )
            
            graphql_data = graphql_response.json()
            
            # Display REST API rate limits
            print("\n📊 GitHub REST API Rate Limits:")
            rest_limits = rest_data.get("resources", {})
            
            rest_table = []
            for category, data in rest_limits.items():
                reset_time = datetime.fromtimestamp(data.get("reset", 0)).strftime("%Y-%m-%d %H:%M:%S")
                rest_table.append([
                    category,
                    data.get("limit", "N/A"),
                    data.get("remaining", "N/A"),
                    data.get("used", "N/A"),
                    reset_time
                ])
            
            print(tabulate(rest_table, headers=["Category", "Limit", "Remaining", "Used", "Reset At"], tablefmt="fancy_grid"))
            
            # Display GraphQL API rate limits
            print("\n📊 GitHub GraphQL API Rate Limits:")
            graphql_limits = graphql_data.get("data", {}).get("rateLimit", {})
            
            graphql_table = [[
                graphql_limits.get("limit", "N/A"),
                graphql_limits.get("remaining", "N/A"),
                graphql_limits.get("used", "N/A"),
                graphql_limits.get("resetAt", "N/A")
            ]]
            
            print(tabulate(graphql_table, headers=["Limit", "Remaining", "Used", "Reset At"], tablefmt="fancy_grid"))
    
    asyncio.run(main())

if __name__ == "__main__":
    app()
