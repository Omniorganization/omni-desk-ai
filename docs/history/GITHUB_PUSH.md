# Push Omni-desk-AI to GitHub

This project is prepared for a GitHub repository named `Omni-desk-AI`.

GitHub repository names cannot contain spaces reliably in clone URLs, so the requested display name `Omni desk AI` should be created as repository slug:

```text
Omni-desk-AI
```

## Option A: GitHub CLI

```bash
gh repo create yinyufan0813-cmyk/Omni-desk-AI --public --source=. --remote=origin --push
```

Use `--private` instead of `--public` if needed.

## Option B: Git commands after creating the repo in GitHub UI

```bash
git remote add origin git@github.com:yinyufan0813-cmyk/Omni-desk-AI.git
git branch -M main
git push -u origin main
```
