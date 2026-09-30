# Setting up code signing (maintainers)

Lyrebird's releases are signed through [SignPath Foundation](https://signpath.org/), which provides free code signing to open-source projects. The CI workflow already has the signing steps. They switch on by themselves once the setup below is done, and until then releases are published unsigned, as before.

## 1. Before applying

- Turn on **multi-factor authentication** for your GitHub account. SignPath requires it for everyone in the [signing roles](../README.md#code-signing-policy), and you'll need it on your SignPath account too.
- Have a published release. v0.1.0 already counts.
- The README already contains the required **Code signing policy** and **Privacy** sections. Keep the team roles there up to date if other people join.

## 2. Apply

Apply at <https://signpath.org/apply> with:

- **Project:** Lyrebird, <https://github.com/zebadrabbit/Lyrebird>
- **License:** MIT
- **What gets signed:** `Lyrebird.exe`, a PyInstaller-built launcher. It's built by GitHub Actions from tagged commits.
- **Build system:** GitHub Actions (github.com)

SignPath Foundation reviews the project, which can take a while.

## 3. Configure the SignPath project

Once you're approved, set up the project in SignPath. The CI workflow expects these names; if you pick different ones, change them in `.github/workflows/ci.yml`.

| Setting | Value |
|---|---|
| Project slug | `lyrebird` |
| Signing policy slug | `release-signing` |
| Artifact configuration slug | `default`, with the contents of [`.signpath/artifact-configuration.xml`](../.signpath/artifact-configuration.xml) |
| Trusted build system | GitHub.com, linked to `zebadrabbit/Lyrebird` |

Then create an **API token** for CI, for a CI user that is allowed to submit signing requests for the `release-signing` policy.

## 4. Connect GitHub

In the GitHub repo, go to **Settings → Secrets and variables → Actions** and add:

- **Secret** `SIGNPATH_API_TOKEN`: the API token from step 3.
- **Variable** (not a secret) `SIGNPATH_ORGANIZATION_ID`: your SignPath organization ID. Setting this variable is what switches signing on.

## 5. Release

1. Bump `VERSION` in `lyrebird.py`, commit, then tag and push, for example `git tag v0.3.0 && git push origin v0.3.0`. CI refuses to release if the tag and `VERSION` don't match.
2. CI builds the app and uploads it to SignPath.
3. SignPath asks an approver (you) to approve the signing request. CI waits up to an hour.
4. After approval, CI downloads the signed `Lyrebird.exe` and checks the signature. It then zips the app and attaches the zip to the GitHub release.

To check a download: right-click `Lyrebird.exe` → **Properties** → **Digital Signatures**.
