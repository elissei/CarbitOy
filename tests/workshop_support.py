"""Explicit fictional memberships/scopes for isolated regression databases only."""

from uuid import NAMESPACE_OID, uuid5

from webapp.database import WorkRepository as ProductionWorkRepository
from webapp.workshop_context import TrustedWorkshopContext, WorkshopPermission


TEST_ORGANIZATION = str(uuid5(NAMESPACE_OID, "carbit.tests.organization"))
TEST_WORKSHOP = str(uuid5(NAMESPACE_OID, "carbit.tests.workshop"))
TEST_STORE = str(uuid5(NAMESPACE_OID, "carbit.tests.offline-identities"))


def test_context(label="offline-fixture", permissions=frozenset(WorkshopPermission)):
    return TrustedWorkshopContext(
        str(uuid5(NAMESPACE_OID, label)), permissions, auth_store_id=TEST_STORE,
        organization_id=TEST_ORGANIZATION, workshop_id=TEST_WORKSHOP, membership_version=1,
    )


test_context.__test__ = False


def seed_test_scope(repository):
    with repository._connect() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO repair_organizations VALUES (?, 'Fictional test repair organization',1) ON CONFLICT DO NOTHING",
                           (TEST_ORGANIZATION,))
        connection.execute("INSERT INTO workshops VALUES (?,?,'Fictional test workshop','ACTIVE') ON CONFLICT DO NOTHING",
                           (TEST_WORKSHOP, TEST_ORGANIZATION))
        for label in ("offline-fixture", "test-employee-stable-id", "pricing-test-employee",
                      "another-test-employee", "trusted-reader"):
            context = test_context(label)
            connection.execute("INSERT INTO workshop_principals VALUES (?,?) ON CONFLICT DO NOTHING",
                               (context.auth_store_id, context.employee_id))
            connection.execute("INSERT INTO workshop_memberships VALUES (?,?,?,1,1) ON CONFLICT DO NOTHING",
                               (context.auth_store_id, context.employee_id, TEST_WORKSHOP))
        # These independent legacy fixtures explicitly authorize their test data.
        connection.execute("""INSERT INTO inquiry_workshop_assignments
            SELECT id,?,? FROM work_inquiries WHERE id NOT IN
            (SELECT inquiry_id FROM inquiry_workshop_assignments)""", (TEST_WORKSHOP, TEST_ORGANIZATION))


class ScopedTestRepository(ProductionWorkRepository):
    """Fixture adapter passes an explicit seeded context, never used by production."""
    def initialize(self):
        super().initialize()
        seed_test_scope(self)

    def create_work(self, **kwargs):
        return super().create_work(**({"context": test_context()} | kwargs))

    def list_works(self, **kwargs):
        return super().list_works(**({"context": test_context()} | kwargs))

    def get_work(self, work_id, **kwargs):
        return super().get_work(work_id, **({"context": test_context()} | kwargs))

    def record_labour(self, **kwargs):
        return super().record_labour(**({"context": test_context()} | kwargs))
