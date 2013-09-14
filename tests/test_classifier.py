import testtools
import elasticRecheck


class TestClassifier(testtools.TestCase):

    def setUp(self):
        super(TestClassifier, self).setUp()
        self.classifier = elasticRecheck.Classifier()

    def test_read_qeuries_file(self):
        self.assertNotEqual(self.classifier.queries, None)

    def test_elasticSearch(self):
        self.classifier.test()
        self.classifier.last_failures()
        #self.assertFalse(True)

    def test_classify(self):
        bug_number = self.classifier.classify('43258', '13',
            ' blah http://logs.openstack.org/58/43258/13/check/gate-tempest-devstack-vm-neutron/55a7887')
        self.assertEqual(bug_number, '1211915')

