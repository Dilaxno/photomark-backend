# Push only backend/ to origin/main using git subtree from repo root
# Usage (run from d:\Software\backend):
#   git add -A
#   git commit -m "Your backend changes"
#   ./push-backend.ps1

# Delete existing temporary subtree branch if present (ignore errors)
git -C .. branch -D backend-only 2>$null

# Create subtree branch with only backend/ contents at repo root
git -C .. subtree split --prefix=backend -b backend-only

# Force-push subtree branch to overwrite origin/main
git -C .. push -f origin backend-only:main
