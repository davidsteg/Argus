# AGENTS.md - Development Rules

## Versioning & Releases
- Maintain a CHANGELOG.md with version history and release notes
- Every git commit must create a corresponding git tag/release
- Tag format: v{major}.{minor}.{patch} (e.g., v0.1.0, v0.1.1, v1.0.0)
- Update CHANGELOG.md before each release commit

## Code Quality
- No spaghetti code: clean, readable, well-structured code only
- Fix issues properly - no dirty workarounds or temporary hacks
- Follow single responsibility principle for modules and functions
- Maintain documentation (README.md, inline docs where needed)
- Keep shared code truly shared - no duplicated logic

## Documentation
- README.md must always be up-to-date
- Inline code documentation for complex logic
- Keep CHANGELOG.md current with every release
- Update API documentation if endpoints change

## Git Workflow
- Commit often with clear, descriptive messages
- Tag every commit as a release
- Use semantic versioning for tags
