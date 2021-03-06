#!/usr/bin/env python

# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


import gerritlib.gerrit
import pyelasticsearch
import urllib2

import ConfigParser
import copy
import json
import logging
import sys
import time

logging.basicConfig()


class Stream(object):
    """Gerrit Stream.

    Monitors gerrit stream looking for tempest-devstack failures.
    """

    def __init__(self, user, host, key, thread=True):
        port = 29418
        self.gerrit = gerritlib.gerrit.Gerrit(host, user, port, key)
        if thread:
            self.gerrit.startWatching()

    def get_failed_tempest(self):
        while True:
            event = self.gerrit.getEvent()
            if event.get('type', '') != 'comment-added':
                continue
            username = event['author'].get('username', '')
            if (username == 'jenkins' and
                    "Build failed.  For information on how to proceed" in
                    event['comment']):
                found = False
                for line in event['comment'].split('\n'):
                    if "FAILURE" in line and ("python2" in line or "pep8" in line):
                        # Unit Test Failure
                        found = False
                        break
                    if "FAILURE" in line and "tempest-devstack" in line:
                        url = [x for x in line.split() if "http" in x][0]
                        if RequiredFiles.files_at_url(url):
                            found = True
                if found:
                    return event
                continue

    def leave_comment(self, project, commit, bug=None):
        if bug:
            bug_url = 'https://bugs.launchpad.net/bugs/%s' % bug
            message = ("I noticed tempest failed, I think you hit bug %s" %
                       bug_url)
        else:
            message = ("I noticed tempest failed, refer to: "
                       "https://wiki.openstack.org/wiki/"
                       "GerritJenkinsGithub#Test_Failures")
        self.gerrit.review(project, commit, message)


class Classifier():
    """Classify failed tempest-devstack jobs based.

    Given a change and revision, query logstash with a list of known queries
    that are mapped to specific bugs.
    """
    ES_URL = "http://logstash.openstack.org/elasticsearch"
    targeted_template = {
            "sort": {
                "@timestamp": {"order": "desc"}
                },
            "query": {
                "query_string": {
                    "query": '%s AND @fields.build_change:"%s" AND @fields.build_patchset:"%s"'
                    }
                }
            }
    files_ready_template = {
            "sort": {
                "@timestamp": {"order": "desc"}
                },
            "query": {
                "query_string": {
                    "query": '@fields.build_status:"FAILURE" AND @fields.build_change:"%s" AND @fields.build_patchset:"%s"'
                    }
                },
            "facets": {
                "tag": {
                    "terms": {
                        "field": "@fields.filename",
                        "size": 80
                        }
                    }
                }
            }
    ready_template = {
            "sort": {
                "@timestamp": {"order": "desc"}
                },
            "query": {
                "query_string": {
                    "query": '@tags:"console.html" AND (@message:"Finished: FAILURE") AND @fields.build_change:"%s" AND @fields.build_patchset:"%s"'
                    }
                }
            }
    general_template = {
            "sort": {
                "@timestamp": {"order": "desc"}
                },
            "query": {
                "query_string": {
                    "query": '%s'
                    }
                }
            }

    queries = None

    def __init__(self, queries):
        self.es = pyelasticsearch.ElasticSearch(self.ES_URL)
        self.queries = json.loads(open(queries).read())
        self.queries_json = queries
        self.log = logging.getLogger("recheckwatchbot")

    def _apply_template(self, template, values):
        query = copy.deepcopy(template)
        query['query']['query_string']['query'] = query['query']['query_string']['query'] % values
        return query

    def test(self):
        query = self._apply_template(self.targeted_template, ('@tags:"console.html" AND @message:"Finished: FAILURE"', '34825', '3'))
        results = self.es.search(query, size='10')
        print results['hits']['total']
        self._parse_results(results)

    def last_failures(self):
        for x in self.queries:
            self.log.debug("Looking for bug: https://bugs.launchpad.net/bugs/%s" % x['bug'])
            query = self._apply_template(self.general_template, x['query'])
            results = self.es.search(query, size='10')
            self._parse_results(results)

    def _parse_results(self, results):
        for x in results['hits']['hits']:
            try:
                change = x["_source"]['@fields']['build_change']
                patchset = x["_source"]['@fields']['build_patchset']
                self.log.debug("build_name %s" % x["_source"]['@fields']['build_name'])
                self.log.debug("https://review.openstack.org/#/c/%(change)s/%(patchset)s" % locals())
            except KeyError:
                self.log.debug("build_name %s" % x["_source"]['@fields']['build_name'])

    def classify(self, change_number, patch_number, comment):
        """Returns either None or a bug number"""
        #Reload each time
        self.queries = json.loads(open(self.queries_json).read())
        #Wait till Elastic search is ready
        self._wait_till_ready(change_number, patch_number, comment)
        for x in self.queries:
            self.log.debug("Looking for bug: https://bugs.launchpad.net/bugs/%s" % x['bug'])
            query = self._apply_template(self.targeted_template, (x['query'],
                    change_number, patch_number))
            results = self.es.search(query, size='10')
            if self._urls_match(comment, results['hits']['hits']):
                return x['bug']

    def _wait_till_ready(self, change_number, patch_number, comment):
        query = self._apply_template(self.ready_template, (change_number,
            patch_number))
        while True:
            try:
                results = self.es.search(query, size='10')
            except pyelasticsearch.exceptions.InvalidJsonResponseError:
                # If ElasticSearch returns an error code, sleep and retry
                #TODO(jogo): if this works pull out search into a helper function that  does this.
                print "UHUH hit InvalidJsonResponseError"
                time.sleep(20)
                continue
            if (results['hits']['total'] > 0 and
                    self._urls_match(comment, results['hits']['hits'])):
                break
            else:
                time.sleep(40)
        query = self._apply_template(self.files_ready_template, (change_number,
            patch_number))
        while True:
            results = self.es.search(query, size='80')
            files = results['facets']['tag']['terms']
            files = [x['term'] for x in files]
            required_files = [
                    'console.html',
                    'logs/screen-key.txt',
                    'logs/screen-n-api.txt',
                    'logs/screen-n-cpu.txt',
                    'logs/screen-n-sch.txt',
                    'logs/screen-c-api.txt',
                    'logs/screen-c-vol.txt'
                    ]
            missing_files = [x for x in required_files if x not in files]
            if len(missing_files) is 0:
                break
            else:
                time.sleep(40)
        # Just because one file is parsed doesn't mean all are, so wait a
        # bit
        time.sleep(20)

    def _urls_match(self, comment, results):
        for result in results:
            url = result["_source"]['@fields']['log_url']
            if RequiredFiles.prep_url(url) in comment:
                return True
        return False


class RequiredFiles(object):

    required_files = [
        'console.html',
        'logs/screen-key.txt',
        'logs/screen-n-api.txt',
        'logs/screen-n-cpu.txt',
        'logs/screen-n-sch.txt',
        'logs/screen-c-api.txt',
        'logs/screen-c-vol.txt'
    ]

    @staticmethod
    def prep_url(url):
        if isinstance(url, list):
            # The url is sometimes a list of one value
            url = url[0]
        if "/logs/" in url:
            return '/'.join(url.split('/')[:-2])
        return '/'.join(url.split('/')[:-1])

    @staticmethod
    def files_at_url(url):
        for f in RequiredFiles.required_files:
            try:
                urllib2.urlopen(url + '/' + f).code
            except urllib2.HTTPError:
                # File does not exist at URL
                return False
        return True


def main():

    config = ConfigParser.ConfigParser()
    if len(sys.argv) is 2:
        config_path = sys.argv[1]
    else:
        config_path = 'elasticRecheck.conf'
    config.read(config_path)
    user = config.get('gerrit', 'user', 'jogo')
    host = config.get('gerrit', 'host', 'review.openstack.org')
    queries = config.get('gerrit', 'query_file', 'queries.json')
    key = config.get('gerrit', 'key')
    classifier = Classifier(queries)
    stream = Stream(user, host, key)
    while True:
        event = stream.get_failed_tempest()
        change = event['change']['number']
        rev = event['patchSet']['number']
        print "======================="
        print "https://review.openstack.org/#/c/%(change)s/%(rev)s" % locals()
        bug_number = classifier.classify(change, rev, event['comment'])
        if bug_number is None:
            print "unable to classify failure"
        else:
            print "Found bug: https://bugs.launchpad.net/bugs/%s" % bug_number

if __name__ == "__main__":
    main()
