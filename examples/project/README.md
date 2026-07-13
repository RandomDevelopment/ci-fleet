# Project Contract Example

These files demonstrate the required shape of a project-owned Docker test environment. They are not intended to be copied without review.

## Files

- `Dockerfile.test`: example project test image.
- `compose.ci.yaml`: internal test and database services without fixed container names or host ports.
- `scripts/ci/run.sh`: standard fast/full entrypoint with run-scoped naming and cleanup.

## Adapting a project

1. Copy the relevant files into the project.
2. Make the entrypoint executable:

   ```bash
   chmod +x scripts/ci/run.sh
   ```

3. Replace the example Node image with the project's actual runtime image.
4. Pin all production base and service images to reviewed versions or digests.
5. Replace the example database credentials with generated, CI-only values when appropriate.
6. Implement `scripts/test-fast.sh` and `scripts/test-full.sh`, or make the entrypoint delegate to existing project scripts.
7. Run both suites locally.
8. Complete the [Project Compliance Checklist](../../docs/COMPLIANCE-CHECKLIST.md).

## Existing Dockerized projects

Do not rewrite established orchestration merely to match the example. Add the standard `scripts/ci/run.sh` adapter and delegate to the existing tested Docker suite.

## Projects using host runtimes

Move dependency installation, compilation, tests, and smoke validation into `Dockerfile.test`. The shared runner must not need the project's language runtime.
