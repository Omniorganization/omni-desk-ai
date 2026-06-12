# Push Omni-desk-AI to GitHub

This project is prepared for a GitHub repository named `Omni-desk-AI`.

GitHub repository names cannot contain spaces reliably in clone URLs, so the requested display name `Omni desk AI` should be created as repository slug:

```text
Omni-desk-AI
```

## Option A: GitHub CLI

```bash
git init -b main
git add .
git commit -m "Initial OmniDesk Agent import"
gh auth login -h github.com -w -s repo
gh repo create yinyufan0813-cmyk/Omni-desk-AI --public --source=. --remote=origin --push
```

Use `--private` instead of `--public` if needed.

If `gh auth status -h github.com` reports an invalid token, re-run the `gh auth login` command above before pushing.

OmniDesk also includes a non-mutating GitHub diagnostics command:

```bash
omnidesk validate-github --config examples/config.yaml
```

For AI-created pull requests, push the head branch before creating the PR:

```bash
git push -u origin ai/<branch>
omnidesk validate-github --config examples/config.yaml --head ai/<branch>
```

## Option B: Git commands after creating the repo in GitHub UI

```bash
git init -b main
git add .
git commit -m "Initial OmniDesk Agent import"
git remote add origin git@github.com:yinyufan0813-cmyk/Omni-desk-AI.git
git branch -M main
git push -u origin main
```
