# GitHub PR Conformance

A simple Python tool to analyze GitHub repositories and identify commits that were pushed directly to the default branch without going through a pull request.

## Installation

1. Clone this repository
2. Create a `.env` file with your GitHub token:
   ```
   GITHUB_TOKEN=your_github_token_here
   ```
3. `uv run conformance.py <owner> <repo>`