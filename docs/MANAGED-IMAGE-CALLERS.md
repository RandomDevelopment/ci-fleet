# Managed-image caller coverage

This records the complete caller review for the portable managed-image protocol.
See [managed images](MANAGED-IMAGES.md) for the contract and native acceptance gates.
The caller IDs are review references, not configuration or evidence identities.

- C01, `build-managed-images.sh staged verification`. Portable source calls the pair content verifier before retaining staged tags. Opaque sources keep exact ID and revision checks.
- C02, `build-managed-images.sh publication/cleanup`. Portable publication tags immutable R. Failure still removes only its own staging tags and builder.
- C03, `installer build_managed_images`. Passes candidate metadata into the selected release build helper. Existing release tool pins and two-tag manifest stay intact.
- C04, `controller_environment_matches`. Expects the portable runner runtime ID, retaining source-specific old Compose behavior.
- C05, `runtime_matches`. Inspects verified controller R and compares the exact live container image ID. No config/manifest/root alias set is accepted.
- C06, `managed_images_match`. Revalidates warm pair proof, then checks canonical tags against bound R. Opaque sources retain their original predicate.
- C07, `managed_image_ids_configured`. Still requires both reviewed scalar fields. It does not select digest semantics or permit image-field removal.
- C08, `drift_count/build_candidate/activate_candidate`. Hydrates candidate fields/proof before exact comparisons. Controlled preparation proves both roles before Compose validation, tag publication, environment install or drain. Activation installs proof before health inside checkpoint recovery coverage.
- C09, `capture_legacy_image_ids`. Keeps local legacy captures as exact opaque values. Portable environments already contain reviewed C and derived R, so capture preserves them.
- C10, `load_checkpoint_images`. Retains exact original tag/live IDs and absent sentinels. No identity normalization is applied to raw maps.
- C11, `make_checkpoint installed capture`. Authenticates original environment and install metadata before copying can confer checkpoint ownership. Reuses warm proof or re-proves the same captured R into the new checkpoint, while recording raw tag/live mappings separately before completion.
- C12, `make_checkpoint fresh capture`. Preserves original candidate-tag absence/raw IDs before preparation. Recovery still validates those exact objects.
- C13, `managed_image_objects_match`. Portable checks use protected pair proof and exact R inspection without exports. Older sources still inspect their opaque scalars.
- C14, `restore_reviewed_image_tags`. Requires both objects first. Portable tags use R, while policy labels and digest fields keep C.
- C15, `preflight_checkpoint_images`. Portable proof and every retained raw ID must be available before drain. Missing/stale proof or roots require controlled import/reproof or reinstall. No checkpoint rewrite.
- C16, `checkpoint_images_reproducible/rebuild_checkpoint_images`. Preserves old release-specific capability/build dispatch and at-most-once reconstruction. Mutable legacy images never rebuild. Portable rollback requires its exact retained roots instead of guessing a reconstructed root.
- C17, `retag_checkpoint_image`. Keeps exact raw object selection, post-tag ID equality, daemon-versus-absence distinction and return code 2.
- C18, `restore_checkpoint evidence gates`. Formats 2/3 and unversioned gates retain ownership, permissions, original-value and pre-drain checks. Portable proof travels in existing install metadata.
- C19, `restore_checkpoint retained/fresh tags`. Captures and preflights raw tag/live IDs and absence separately. Inherited installed restoration later republishes reviewed tags, so it does not guarantee every final raw tag mapping or absence. Fresh no-install restoration and the deferred controller-tag case retain their specific exact-ID checks.
- C20, `restore_checkpoint final verification`. Reviewed image gates share portable proof. Deferred prior controller-tag restoration still requires its exact captured raw ID.
- C21, `staged_runtime_matches`. Keeps image-free staging on the retained runtime source and environment. Manager capability never reinterprets incumbent images.
- C22, `controller run`. Inspects the supplied immutable R and checks its exact ID, source revision and Linux architecture. It never accesses root proof or exports content.
- C23, `startRunner/recoverRunners`. ContainerCreate already uses the supplied runner reference and now receives verified R. Recovery ownership and active-work behavior remain unchanged.
- C24, `Compose services`. Controller and proxy launch by controller R; runner input uses runner R. Reviewed labels retain policy C. Missing runtime fields fail rendering.
- C25, `desired_state build_rendered_env`. Retains engine/architecture selection and scalar C. Runtime fields are allowlisted only as derived rendered output, never host input.
- C26, `explicit_recovery.verify`. Preserves additive image publication and immutable reviewed engine-image entries. Historical catalogs cannot be replaced by a different ID kind.
- C27, `cleanup protected_images`. Reads current/checkpoint proof and protects authenticated C/R/manifest plus existing raw references. Missing/untrusted/stale proof defers image/cache removal.
- C28, `image_reason/cleanup_storage`. Existing container-use, alias, age, ownership and unknown-use gates consume the expanded protection set. An opaque retained ID absent from complete storage accounting defers removal instead of guessing an alias. Warm proof performs no full export.
- C29, `eligible image removal`. Exact inspected backend-object reference removal remains unforced and no-prune. Coordination, pass and count limits stay unchanged.
- C30, `health container/stale/proxy checks`. Direct cleanup accounting fails closed. The inherited health stale-resource helper counts successful cleanup output but returns zero counts on cleanup failure; health success cannot establish usable image proof or native acceptance.
- C31, `healthcheck entrypoint`. Uses the shared rendered allowlist for derived runtime IDs. It remains read-only and does not repair missing proof.
- C32, `installed drift dispatch`. Existing protected installed-ref/installer check dispatch inherits warm proof and hydration. No new mutation path.
- C33, `remote reconciliation`. Supplies committed current and immediate-parent fleet/active/pending evidence. Approval, final health, protected LKG and transaction outcomes remain separate gates.
- C34, `release completeness/install`. Portable capability requires the verifier in complete trusted runtime/manager trees. Full reviewed-tree installation and digest/permission checks remain intact.
- C35, `controller Dockerfile`. Copies the generic verifier alongside cleanup imports for build-stage tests. No host proof or store mount is packaged.
- C36, `Docker socket proxy`. Raw forwarding and existing mutation coordination remain unchanged. API negotiation does not normalize IDs.
- C37, `installer staging cleanup`. Only per-invocation prepared tags are removed on exit. No global pruning or content-alias expansion.
- C38, `deployer artifact verification`. Registry repository@digest policy remains separate and unchanged. Managed-image config proof grants no deployment authority.

Offline verification covers the parser/content binding, pair publication order,
source-specific opaque versus portable selection, distinct legacy pairs, runtime
comparison, checkpoint preflight, drift hydration, cleanup protection and controller
startup. Existing transaction regressions explicitly model opaque runtime sources;
portable parser and caller regressions add coverage without deleting their cases.

Full privileged transaction/recovery fixtures and native classic/containerd export,
import, launch, cleanup, populated-store and resource measurements remain required
on supervised disposable workers. This document does not record their acceptance.

## Standalone template follow-up

The standalone configuration template needs the matching generic active/pending
validator contract and caller flags, scalar descriptions, initializer help,
capability evidence, policy tests and adoption/recovery guidance. Preserve its
historical release/compatibility records and published fixture identities. Document
first installation without optional policy or active evidence, then observed
runtime activation, evidence-only integration and later network policy. Source
support alone cannot establish active runtime evidence. Require explicit
incumbent records for image-free manager staging.
Stage `portable_managed_image_ids` evidence against the actual reviewed merged engine,
retain old catalogs, and validate ordinary upgrades plus image-free bootstrap and
final evidence-only promotion. Required checks include its complete policy,
compatibility, initializer/updater/release checks and exact source/base suite.
Root tracks the standalone gap in [template issue #43](https://github.com/RandomDevelopment/ci-fleet-config-template/issues/43).
The issue records the pending engine work; standalone implementation and publication
remain separate and incomplete.
