import unittest

from oke_hpc_mgmt.backends.kubernetes import parse_instance_ocid
from oke_hpc_mgmt.models import NodeInfo


class ModelTests(unittest.TestCase):
    def test_parse_instance_ocid_from_oci_provider_id(self):
        provider_id = "oci://ocid1.instance.oc1.iad.exampleuniqueid"

        self.assertEqual(parse_instance_ocid(provider_id), "ocid1.instance.oc1.iad.exampleuniqueid")

    def test_parse_instance_ocid_from_raw_ocid(self):
        provider_id = "ocid1.instance.oc1.iad.exampleuniqueid"

        self.assertEqual(parse_instance_ocid(provider_id), "ocid1.instance.oc1.iad.exampleuniqueid")

    def test_node_status_ready_and_unschedulable(self):
        node = NodeInfo(k8s_name="10.0.0.1", ready=True, schedulable=False)

        self.assertEqual(node.status, "SchedulingDisabled")


if __name__ == "__main__":
    unittest.main()
