# systemd configuration

Copy the CPU and watcher environment examples to their corresponding paths in
`/etc/mainrag` with owner-only access. For an existing installation, use the
same home, working directory and configuration directory as the current service.
The services use the settings in `/etc/mainrag/mainrag-cpu.env` and
`/etc/mainrag/mainrag-watcher.env`.

Place any access settings for the watcher in a private systemd drop-in. Preserve
the source's supplementary group and read-only source directory if the existing
installation uses them. Keep existing API URL and sync interval overrides.

Deploy the environment files and drop-in before loading the updated units.
Verify the CPU service's normal operation and the watcher's read access to its
configured source during the maintenance window.
