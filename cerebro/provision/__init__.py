"""cerebro.provision: the provisioning layer.

    plan.py       cerebro.yaml + every adapter's units()/jobs() -> the complete list of UnitSpec / JobSpec
    operator.py   the kopf operator that scales idle docs/code Deployments to zero (kubernetes target)

The renderers and runtime drivers are adapters: cerebro.adapters.provision.compose / .kubernetes.
"""
