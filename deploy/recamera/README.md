# reCamera Pro package integration

`official/` contains unmodified Apache-2.0 official App Center builder files.
The upstream revision and each source digest are pinned in `official/source.json`;
the application checks these before running the builder. The reviewed revision
adds optional browser overlays/render rules and typed Kit metrics, retains the
scheduled model artifact authorization contract, and adds memory admission on
device. CVSprout keeps conservative 200 MiB archive / 400 MiB unpacked limits.

`templates/` is a distribution snapshot of the application sources maintained in
`seeed-recamera-pro-ext-api/apps/cvsprout-capture` and `apps/cvsprout-inference`.
No model binary, device credential or machine path is committed here. The server
fills the inference model, labels, version and content hashes from an immutable
completed conversion. Do not edit a deployed RKNN file in place.

After changing the SDK app source, synchronize the templates using:

```sh
python3 scripts/sync_recamera_templates.py --sdk-root /path/to/seeed-recamera-pro-ext-api
```

Run the CVSprout package/API tests and the SDK app tests, then build and smoke-test
the final archives using the `recamera-pysdk` skill's official packaging helpers.
Runtime users do not need that skill or a local SDK checkout to generate packages.

`compose.ssh.yaml` is an optional read-only SSH credential mount for direct device
deployment. See [the setup guide](../../docs/recamera-pro.md).
