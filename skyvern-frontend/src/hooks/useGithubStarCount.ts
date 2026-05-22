import { useQuery } from "@tanstack/react-query";

const GITHUB_REPO_API_URL = "https://api.github.com/repos/skyvern-ai/skyvern";

type GithubRepoResponse = {
  stargazers_count: number;
};

type Options = {
  enabled?: boolean;
};

const starCountFormatter = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 1,
});

function useGithubStarCount({ enabled = true }: Options = {}) {
  return useQuery<number>({
    queryKey: ["githubStarCount", "skyvern-ai/skyvern"],
    queryFn: async () => {
      const response = await fetch(GITHUB_REPO_API_URL, {
        headers: { Accept: "application/vnd.github+json" },
      });
      if (!response.ok) {
        throw new Error(`GitHub API ${response.status}`);
      }
      const data: GithubRepoResponse = await response.json();
      return data.stargazers_count;
    },
    enabled,
    staleTime: 60 * 60 * 1000,
    gcTime: 24 * 60 * 60 * 1000,
    retry: false,
    refetchOnWindowFocus: false,
  });
}

export { starCountFormatter, useGithubStarCount };
