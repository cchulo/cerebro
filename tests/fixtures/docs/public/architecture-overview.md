# Architecture overview (exported)

The commerce platform is three services: checkout (Python), ledger (Go) and the notifier. All of them run on the
shared Kubernetes cluster and use the pg-main Postgres cluster. Document id FILES-ARCH-6601.
