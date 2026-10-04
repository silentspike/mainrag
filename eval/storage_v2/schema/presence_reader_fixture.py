"""Release the cluster role created by an owned presence-reader fixture."""
import subprocess


def register_presence_role_cleanup(stack, socket):
    # Register only after migration 142 committed its create-if-absent guard.
    # The fixture teardown drops its database before closing this stack; this
    # callback then runs before an owned temporary PostgreSQL server stops.
    def cleanup():
        subprocess.run(
            ["psql", "-X", "--no-psqlrc", "-qAt", "--set=ON_ERROR_STOP=1",
             "--host", str(socket), "--dbname", "postgres", "--command",
             "DROP ROLE mainrag_v2_presence_owner"],
            check=True, capture_output=True, text=True,
        )
        print("Owned presence-reader fixture role removed", flush=True)

    stack.callback(cleanup)
