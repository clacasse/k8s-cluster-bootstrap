# Private Apps Repo

Argo CD watches this repository for your private/non-shareable applications. It
was scaffolded by [`k8s-cluster-bootstrap`](https://github.com/clacasse/k8s-cluster-bootstrap)'s
`cluster_manager.py private-apps scaffold` command.

## How it works

A root Argo `Application` (registered once by `cluster_manager.py private-apps setup`)
points at the `apps/` directory of this repo with `directory.recurse: true`. Every
`Application` manifest committed under `apps/**` is picked up automatically — this
is the standard "app-of-apps" pattern.

You can either:

- **Put inline manifests directly under `apps/<name>/`** (raw Deployments, Services, etc.) — simpler for small apps.
- **Commit child `Application` manifests that point at other repos or Helm charts** — better for bigger apps whose own source tree lives elsewhere (e.g., a private app repo with its own Helm chart).

Both styles coexist.

## Layout

```
apps/
├── <app-a>/
│   └── application.yaml            # Child Argo Application pointing elsewhere
└── <app-b>/
    ├── deployment.yaml             # Inline manifests
    ├── service.yaml
    └── ingress.yaml
```

The `EXAMPLE-application.yaml.disabled` file shows the shape of a child Application
manifest. Rename (drop the `.disabled` suffix) and edit to activate.

## Adding a new app

1. Create `apps/<name>/` with your manifests or a child `Application`.
2. Commit and push.
3. Argo CD picks it up on its next reconciliation cycle (usually within a minute).
4. Check status: `kubectl -n argocd get applications` on the cluster.

## Removing an app

Delete the directory, commit, push. With `syncPolicy.automated.prune: true` on
the root Application (the default), Argo will clean up the resources.

## Secrets

This repo is private, but **do not commit plaintext secrets** even so. Patterns:

- Create Secrets imperatively via `kubectl` or a bootstrap script (same pattern
  `cluster_manager.py` uses for cluster-wide secrets).
- Once you introduce Infisical / ExternalSecrets / SealedSecrets on the cluster,
  migrate to that.

## Unregistering

```bash
cluster_manager.py private-apps unregister
```

Removes the Argo Application, AppProject, and repository credential from the
cluster. Does not delete the deploy key on this repo — remove that via the
GitHub/GitLab UI.
