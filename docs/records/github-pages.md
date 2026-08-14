# GitHub Pages demos

`.github/workflows/pages.yml` validates the project, renders the existing ingest and fulfillment static HTML demos, and deploys them with GitHub’s native Pages actions. The landing page is `docs/index.md` rendered by MkDocs into `index.html`; this meets the need for shareable demos without adding a server, framework, or dependency.
