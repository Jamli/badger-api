from django.test import TestCase
from django.conf import settings
from django.contrib.auth.models import User, Permission, ContentType

from common.models import Project, Settings
from testreport.models import TestPlan
from testreport.models import Launch
from testreport.models import Build
from testreport.models import Bug
from testreport.models import INIT_SCRIPT, ASYNC_CALL
from testreport.models import PASSED, FAILED, SKIPPED, BLOCKED
from testreport.models import STOPPED

from stages.models import Stage

from metrics.models import Metric, MetricValue

from djcelery.models import PeriodicTask, CrontabSchedule

from testreport.tasks import update_bugs
from testreport.tasks import cleanup_database

from django.test.utils import override_settings

from django.utils import timezone
from datetime import timedelta
from datetime import datetime

import requests_mock
import json
import random
import os
import base64


class AbstractEntityApiTestCase(TestCase):
    allowed_codes = [200, 201, 400, 403, 404, 415, ]

    user_login = 'user'
    user_plain_password = 'qweqwe'

    def setUp(self):
        User.objects.create_user(username=self.user_login,
                                 email='user@domain.tld',
                                 password=self.user_plain_password)
        self.client.login(username=self.user_login,
                          password=self.user_plain_password)

    def _create_project(self, name):
        data = {'name': name}
        return self._call_rest('post', 'projects/', data)

    def _call_rest(self, method, url,
                   data=None, content_type='application/json'):
        http_method = getattr(self.client, method)
        url = '/{0}/{1}'.format(settings.CDWS_API_PATH, url)
        request_data = None
        if data is not None:
            if content_type == 'application/json':
                request_data = json.dumps(data)
            else:
                request_data = data

        response = http_method(url,
                               data=request_data,
                               content_type=content_type)

        if response.status_code not in self.allowed_codes:
            raise IndexError('HTTP status code {0} out of range '
                             'of expected values.'.
                             format(response.status_code))

        if not response.content:
            return response

        if content_type == u'application/json':
            return json.loads(response.content.decode('utf-8',
                                                      errors='replace'))
        else:
            return response


class ProjectApiTestCase(AbstractEntityApiTestCase):
    def setUp(self):
        super(ProjectApiTestCase, self).setUp()
        self._set_user_permissions()

    def _get_projects(self):
        return self._call_rest('get', 'projects/')

    def _set_user_permissions(self):
        content_type = ContentType.objects.get_for_model(Project)
        permissions = Permission.objects.filter(content_type=content_type)
        user = User.objects.get(username=self.user_login)
        for permission in permissions:
            user.user_permissions.add(permission)

    def _remove_user_permission(self, codename):
        user = User.objects.get(username=self.user_login)
        user.user_permissions.remove(
            Permission.objects.get(codename=codename))

    def test_creation(self):
        project = self._create_project('DummyProject')
        projects = self._get_projects()
        self.assertEqual(len(projects['results']), 1)
        self.assertEqual(project, projects['results'][0])

    def test_duplication(self):
        self._create_project('DummyProject')
        self._create_project('DummyProject')
        self.assertEqual(len(Project.objects.all()), 1)

    def test_add_new_setting(self):
        project = Project.objects.create(name='DummyTestProject')
        data = {'key': 'key', 'value': 'value'}
        self._call_rest('post',
                        'projects/{}/settings/'.format(project.id), data)
        setting = Settings.objects.filter(project_id=project.id)
        self.assertEqual(len(setting), 1)
        self.assertEqual(setting[0].key, 'key')
        self.assertEqual(setting[0].value, 'value')

    def test_update_setting(self):
        project = Project.objects.create(name='DummyTestProject')
        Settings.objects.create(key='key', value='value', project=project)
        data = {'key': 'key', 'value': 'new_value'}
        self._call_rest('post',
                        'projects/{}/settings/'.format(project.id), data)
        setting = Settings.objects.filter(project_id=project.id)
        self.assertEqual(len(setting), 1)
        self.assertEqual(setting[0].key, 'key')
        self.assertEqual(setting[0].value, 'new_value')

    def test_delete_setting_by_existent_key(self):
        project = Project.objects.create(name='DummyTestProject')
        Settings.objects.create(key='key', value='value', project=project)
        data = {'key': 'key', 'value': 'value'}
        response = self._call_rest(
            'post', 'projects/{}/settings/delete/'.format(project.id), data)
        self.assertEqual(response['message'], 'ok')

        projects = self._get_projects()
        settings = projects['results'][0]['settings']
        self.assertEqual(len(settings), 0)

    def test_delete_setting_by_nonexistent_key(self):
        project = Project.objects.create(name='DummyTestProject')
        Settings.objects.create(key='key', value='value', project=project)
        data = {'key': 'key1', 'value': 'value'}
        response = self._call_rest(
            'post', 'projects/{}/settings/delete/'.format(project.id), data)
        self.assertEqual(response['message'], 'ok')

        projects = self._get_projects()
        settings = projects['results'][0]['settings']
        self.assertEqual(len(settings), 1)

    def test_update_settings_without_permissions(self):
        self._remove_user_permission('add_project')
        project = Project.objects.create(name='DummyTestProject')
        data = {'key': 'key', 'value': 'value'}
        response = self._call_rest(
            'post', 'projects/{}/settings/'.format(project.id), data)
        self.assertEqual('You do not have permission to perform this action.',
                         response['detail'])


class TestPlanApiTestCase(AbstractEntityApiTestCase):
    def setUp(self):
        project = Project.objects.create(name='DummyTestProject')
        TestPlan.objects.create(name='DummyTestPlan',
                                project=project)
        super(TestPlanApiTestCase, self).setUp()

    def _create_testplan(self, name, project_id,
                         hidden=None, description=None):
        if hidden is not None:
            data = {'name': name, 'project': project_id, 'hidden': hidden}
        elif description is not None:
            data = {'name': name, 'project': project_id,
                    'description': description}
        else:
            data = {'name': name, 'project': project_id}
        return self._call_rest('post', 'testplans/', data)

    def _update_testplan(self, testplan_id, name,
                         hidden=None, main=None,
                         statistic_filter=None, description=None,
                         variable_name=None, variable_value_regexp=None,
                         summary=None, twodays=None):
        data = {'name': name}
        if main is not None:
            data['main'] = main
        if hidden is not None:
            data['hidden'] = hidden
        if statistic_filter is not None:
            data['filter'] = statistic_filter
        if description is not None:
            data['description'] = description
        if variable_name is not None:
            data['variable_name'] = variable_name
        if variable_value_regexp is not None:
            data['variable_value_regexp'] = variable_value_regexp
        if summary is not None:
            data['show_in_summary'] = summary
        if twodays is not None:
            data['show_in_twodays'] = twodays
        return self._call_rest('patch',
                               'testplans/{0}/'.format(testplan_id), data)

    def _tp_execute(self, testplan_id, data):
        return self._call_rest('post',
                               'testplans/{0}/execute/'.format(testplan_id),
                               data)

    def _get_testplans(self, filter=None):
        if filter is not None:
            return self._call_rest('get',
                                   'testplans/custom_list/{}'.format(filter))
        else:
            return self._call_rest('get', 'testplans/')

    def _create_launch_item(self, data):
        return self._call_rest('post', 'launch-items/', data)

    def _get_launch(self, launch_id):
        return self._call_rest('get', 'launches/{}/'.format(launch_id))

    def test_duplication(self):
        project = Project.objects.get(name='DummyTestProject')
        self._create_testplan('DummyTestPlan', project.id)
        self._create_testplan('DummyTestPlan', project.id)
        self.assertEqual(len(self._get_testplans()['results']), 1)

    def test_creation(self):
        project = Project.objects.get(name='DummyTestProject')
        self._create_testplan('DummyTestPlan', project.id)
        self._create_testplan('AnotherDummyTestPlan', project.id)
        self.assertEqual(len(self._get_testplans()['results']), 2)

    def test_hidden_flag(self):
        project = Project.objects.get(name='DummyTestProject')
        data = self._create_testplan('HiddenByDefaultTrue', project.id)
        self.assertTrue(data['hidden'])
        data = self._create_testplan('Force hidden false', project.id, False)
        self.assertFalse(data['hidden'])

    def test_main_flag(self):
        project = Project.objects.get(name='DummyTestProject')
        data = self._create_testplan('MainByDefaultFalse', project.id)
        self.assertFalse(data['main'])

    def test_summary_flag(self):
        project = Project.objects.get(name='DummyTestProject')
        data = self._create_testplan('SummaryByDefaultFalse', project.id)
        self.assertFalse(data['show_in_summary'])

    def test_twodays_flag(self):
        project = Project.objects.get(name='DummyTestProject')
        data = self._create_testplan('TwodaysByDefaultFalse', project.id)
        self.assertFalse(data['show_in_twodays'])

    def test_statistic_filter(self):
        testplan = TestPlan.objects.get(name='DummyTestPlan')
        self.assertEquals(testplan.filter, '')

    def test_execute_success(self):
        test_plan = TestPlan.objects.get(name='DummyTestPlan')

        self._create_launch_item({
            'test_plan': test_plan.id,
            'command': 'touch init_file',
            'type': INIT_SCRIPT,
            'timeout': 10,
        })

        count = random.choice([2, 3, 4, 5])
        for x in range(0, count):
            self._create_launch_item({
                'test_plan': test_plan.id,
                'command': 'touch file_'.format(x),
                'type': ASYNC_CALL,
                'timeout': 10,
            })

        output = self._tp_execute(
            test_plan.id, {'options': {'started_by': 'http://2gis.local/'}})
        launch_id = output['launch_id']
        launch = self._get_launch(launch_id)
        self.assertEqual(len(launch['tasks']), count + 1)
        self.assertTrue(launch['build'])
        self.assertFalse(launch['build']['version'])
        self.assertFalse(launch['build']['hash'])
        self.assertFalse(launch['build']['branch'])

    def test_execute_with_incorrect_items(self):
        test_plan = TestPlan.objects.get(name='DummyTestPlan')

        self._create_launch_item({
            'test_plan': test_plan.id,
            'command': 'touch init_file',
            'type': INIT_SCRIPT,
            'timeout': 10,
        })

        output = self._tp_execute(
            test_plan.id,
            {'options': {'started_by': 'http://2gis.local/'},
             'launch_items': '1,2,3'})
        self.assertIsNotNone(output.get('message'))

    def test_execute_with_items(self):
        test_plan = TestPlan.objects.get(name='DummyTestPlan')

        self._create_launch_item({
            'test_plan': test_plan.id,
            'command': 'touch init_file',
            'type': INIT_SCRIPT,
            'timeout': 10,
        })

        output = self._tp_execute(
            test_plan.id,
            {'options': {'started_by': 'http://2gis.local/'},
             'launch_items': [1, 2, 3]})

        launch_id = output['launch_id']
        launch = self._get_launch(launch_id)
        self.assertEqual(len(launch['tasks']), 1)

    def test_execute_failure(self):
        test_plan = TestPlan.objects.get(name='DummyTestPlan')

        output = self._tp_execute(
            test_plan.id,
            {'options': {'started_by': 'http://2gis.local/'}})
        self.assertIsNotNone(output.get('message'))

    def test_execute_build_options(self):
        test_plan = TestPlan.objects.get(name='DummyTestPlan')

        self._create_launch_item({
            'test_plan': test_plan.id,
            'command': 'touch init_file',
            'type': INIT_SCRIPT,
            'timeout': 10,
        })

        count = random.choice([2, 3, 4, 5])
        for x in range(0, count):
            self._create_launch_item({
                'test_plan': test_plan.id,
                'command': 'touch file_'.format(x),
                'type': ASYNC_CALL,
                'timeout': 10,
            })

        output = self._tp_execute(
            test_plan.id, {'options': {'started_by': 'http://2gis.local/',
                                       'version': '123', 'hash': '123',
                                       'branch': '123'}})
        launch_id = output['launch_id']
        launch = self._get_launch(launch_id)
        self.assertEqual(len(launch['tasks']), count + 1)
        self.assertTrue(launch['build'])
        self.assertEqual(launch['build']['version'], '123')
        self.assertEqual(launch['build']['hash'], '123')
        self.assertEqual(launch['build']['branch'], '123')

    def test_deploy_script_duplication(self):
        test_plan = TestPlan.objects.get(name='DummyTestPlan')
        self._create_launch_item({
            'test_plan': test_plan.id,
            'command': 'touch init_file',
            'type': INIT_SCRIPT,
            'timeout': 10,
        })

        self._create_launch_item({
            'test_plan': test_plan.id,
            'command': 'touch init_file2',
            'type': INIT_SCRIPT,
            'timeout': 10,
        })

        output = self._tp_execute(
            test_plan.id, {'options': {'started_by': 'http://2gis.local/'}})
        launch_id = output['launch_id']
        launch = self._get_launch(launch_id)

        self.assertEqual(len(launch['tasks']), 1, launch['tasks'])

    def test_update_main_flag(self):
        testplan = TestPlan.objects.get(name='DummyTestPlan')
        self.assertFalse(testplan.main)
        data = self._update_testplan(testplan.id, testplan.name, main=True)
        self.assertTrue(data['main'])
        data = self._update_testplan(testplan.id, testplan.name, main=False)
        self.assertFalse(data['main'])

    def test_update_summary_flag(self):
        testplan = TestPlan.objects.get(name='DummyTestPlan')
        self.assertFalse(testplan.show_in_summary)
        data = self._update_testplan(testplan.id, testplan.name, summary=True)
        self.assertTrue(data['show_in_summary'])
        data = self._update_testplan(testplan.id, testplan.name, summary=False)
        self.assertFalse(data['show_in_summary'])

    def test_update_twodays_flag(self):
        testplan = TestPlan.objects.get(name='DummyTestPlan')
        self.assertFalse(testplan.show_in_twodays)
        data = self._update_testplan(testplan.id, testplan.name, twodays=True)
        self.assertTrue(data['show_in_twodays'])
        data = self._update_testplan(testplan.id, testplan.name, twodays=False)
        self.assertFalse(data['show_in_twodays'])

    def test_update_hidden_flag(self):
        testplan = TestPlan.objects.get(name='DummyTestPlan')
        self.assertTrue(testplan.hidden)
        data = self._update_testplan(testplan.id, testplan.name, hidden=False)
        self.assertFalse(data['hidden'])

    def test_update_name(self):
        testplan = TestPlan.objects.get(name='DummyTestPlan')
        data = self._update_testplan(testplan.id, 'NewDummyTestPlan')
        self.assertEquals('NewDummyTestPlan', data['name'])

    def test_update_parameters(self):
        testplan = TestPlan.objects.get(name='DummyTestPlan')
        self.assertEquals(testplan.filter, '')
        data = self._update_testplan(testplan.id, testplan.name,
                                     statistic_filter='regression')
        self.assertEquals(data['filter'], 'regression')

    def test_owner(self):
        login = 'user1'
        password = 'qweqwe'
        User.objects.create_user(username=login,
                                 email='user@domain.tld',
                                 password=password)
        self.client.login(username=login,
                          password=password)
        project = Project.objects.get(name='DummyTestProject')
        data = self._create_testplan('TestplanWithAnotherOwner', project.id)
        user = User.objects.get(username__exact=login)
        self.assertEqual(data['owner'], user.id)

    def test_filter_in(self):
        projects = [self._create_project('Project1'),
                    self._create_project('Project2')]
        testplans = [self._create_testplan('TP1', projects[0]['id']),
                     self._create_testplan('TP1', projects[1]['id'])]
        url = '?project_id__in={0},{1}'.format(projects[0]['id'],
                                               projects[1]['id'])
        data = self._get_testplans(url)
        self.assertEqual(len(data['results']), len(testplans),
                         'Length of results in response not expected. '
                         'Expected "{0}", actual list:"{1}"'.format(
                             len(testplans),
                             json.dumps(data['results'], indent=4)))
        for testplan in testplans:
            self.assertTrue(
                any(tp['name'] == testplan['name'] for tp in data['results']),
                'Testplan "{0}" not found in response "{1}"'.format(
                    json.dumps(testplan['name'], indent=4),
                    json.dumps(data['results'], indent=4)))

    def test_filter_in_empty(self):
        projects = [self._create_project('P1'),
                    self._create_project('P2')]
        testplans = [self._create_testplan('TP1', projects[0]['id']),
                     self._create_testplan('TP1', projects[1]['id'])]

        data = self._get_testplans('?project_id__in=')
        self.assertEqual(len(data['results']), len(testplans) + 1,
                         'Length of results in response not expected. '
                         'Expected "{0}", actual list:"{1}"'.format(
                             len(testplans) + 1,
                             json.dumps(data['results'], indent=4)))

    def test_tp_id_in(self):
        projects = [self._create_project('Project1'),
                    self._create_project('Project2')]
        self._create_testplan('TP1', projects[0]['id'])
        self._create_testplan('TP2', projects[1]['id'])
        self._create_testplan('TP3', projects[1]['id'])

        data = self._get_testplans()
        self.assertEqual(len(data['results']), 4)

        url = '?id__in=1,2'
        data = self._get_testplans(url)
        self.assertEqual(len(data['results']), 2)

    def test_task(self):
        task = self._call_rest('get', 'tasks/{}/'.format(1))
        self.assertEqual('PENDING', task['status'])
        self.assertEqual(None, task['result'])

    def test_default_description(self):
        project = Project.objects.get(name='DummyTestProject')
        data = self._create_testplan('DefaultDescription', project.id)
        self.assertFalse(data['description'])

    def test_description(self):
        project = Project.objects.get(name='DummyTestProject')
        data = self._create_testplan(
            'DefaultDescription', project.id,
            description='Testplan description textfield for DummyTestProject')
        self.assertEqual(data['description'],
                         'Testplan description textfield for DummyTestProject')

    def test_update_description(self):
        testplan = TestPlan.objects.get(name='DummyTestPlan')
        self.assertFalse(testplan.description)
        data = self._update_testplan(testplan.id, testplan.name,
                                     description='Update description')
        self.assertEqual(data['description'], 'Update description')
        data = self._update_testplan(testplan.id, testplan.name,
                                     description='')
        self.assertFalse(data['description'])

    def test_default_variable_settings(self):
        project = Project.objects.get(name='DummyTestProject')
        data = self._create_testplan('DefaultBranchSettings', project.id)
        self.assertFalse(data['variable_name'])
        self.assertFalse(data['variable_value_regexp'])

    def test_update_variable_settings(self):
        testplan = TestPlan.objects.get(name='DummyTestPlan')
        self.assertFalse(testplan.description)
        data = self._update_testplan(testplan.id, testplan.name,
                                     variable_name='BRANCH',
                                     variable_value_regexp='^\d+$')
        self.assertEqual(data['variable_name'], 'BRANCH')
        self.assertEqual(data['variable_value_regexp'], '^\d+$')
        data = self._update_testplan(testplan.id, testplan.name,
                                     variable_name='BRANCH',
                                     variable_value_regexp='')
        self.assertEqual(data['variable_name'], 'BRANCH')
        self.assertEqual(data['variable_value_regexp'], '')


class LaunchApiTestCase(AbstractEntityApiTestCase):
    def setUp(self):
        super(LaunchApiTestCase, self).setUp()
        project = Project.objects.create(name='DummyTestProject')
        TestPlan.objects.create(name='DummyTestPlan', project=project)

    def _create_launch_item(self, data):
        return self._call_rest('post', 'launch-items/', data)

    def _create_launch(self, testplan_id):
        data = {
            'test_plan': testplan_id,
            'started_by': 'http://2gis.local/'
        }
        return self._call_rest('post', 'launches/', data)

    def _get_launch(self, launch_id):
        return self._call_rest('get', 'launches/{}/'.format(launch_id))

    def _terminate_launch(self, testplan_id):
        return self._call_rest(
            'get',
            'launches/{}/terminate_tasks/'.format(testplan_id))

    def get_launches(self):
        return self._call_rest('get', 'launches/')

    def test_creation(self):
        test_plan = TestPlan.objects.get(name='DummyTestPlan')
        launch = self._create_launch(test_plan.id)
        self.assertEqual(len(self.get_launches()['results']), 1)
        self.assertEqual(launch['test_plan'], test_plan.id)
        self.assertFalse(launch['build'])

    def test_termination(self):
        test_plan = TestPlan.objects.get(name='DummyTestPlan')
        self._create_launch_item({
            'test_plan': test_plan.id,
            'command': 'sleep 600',
            'type': INIT_SCRIPT,
            'timeout': 1200,
        })
        launch = self._create_launch(test_plan.id)
        output = self._terminate_launch(launch['id'])
        self.assertEqual(output['message'], 'Termination done.')
        actual_launch = self._get_launch(launch['id'])
        self.assertEqual(actual_launch['state'], STOPPED)

    def test_calculate_counts(self):
        test_plan = TestPlan.objects.get(name='DummyTestPlan')
        launch = self._create_launch(test_plan.id)

        data = [{
            'launch': launch['id'],
            'name': 'DummyTestCase',
            'suite': 'DummyTestSuite',
            'state': PASSED,
            'failure_reason': None,
            'duration': 1
        }, {
            'launch': launch['id'],
            'name': 'SecondDummyTestSuite',
            'suite': 'SecondDummyTestSuite',
            'state': FAILED,
            'failure_reason': 'Exception: Clear message about failure',
            'duration': 5
        }, {
            'launch': launch['id'],
            'name': 'SkipDummyTestSuite',
            'suite': 'SkipDummyTestSuite',
            'state': SKIPPED,
            'failure_reason': None,
            'duration': 0
        }]
        self._call_rest('post', 'testresults/', data)

        response = self._call_rest(
            'get', 'launches/{}/calculate_counts/'.format(launch['id']))
        self.assertEqual('Calculation done.', response['message'])
        actual_launch = self._get_launch(launch['id'])
        self.assertFalse(actual_launch['duration'])
        self.assertEqual(3, actual_launch['counts']['total'])

    def test_update_duration(self):
        test_plan = TestPlan.objects.get(name='DummyTestPlan')
        launch = self._create_launch(test_plan.id)
        self.assertFalse(launch['duration'])
        response = self._call_rest(
            'patch',
            'launches/{0}/'.format(launch['id']), {'duration': 360})
        self.assertEqual(360, response['duration'])

    def test_build_filter(self):
        test_plan = TestPlan.objects.get(name='DummyTestPlan')
        Launch(test_plan=test_plan).save()
        launch2 = Launch(test_plan=test_plan)
        launch2.save()
        Build(launch=launch2, version=123, branch=123, hash=123).save()
        self.assertEqual(len(self.get_launches()['results']), 2)

        response = self._call_rest(
            'get', 'launches/?build__version=123')
        self.assertEqual(len(response['results']), 1)
        self.assertEqual(response['results'][0]['id'], launch2.id)

        response = self._call_rest(
            'get', 'launches/?build__branch=123')
        self.assertEqual(len(response['results']), 1)
        self.assertEqual(response['results'][0]['id'], launch2.id)

        response = self._call_rest(
            'get', 'launches/?build__hash=123')
        self.assertEqual(len(response['results']), 1)
        self.assertEqual(response['results'][0]['id'], launch2.id)

        response = self._call_rest(
            'get', 'launches/?build__version=333')
        self.assertEqual(len(response['results']), 0)

    def test_ids_in_filter(self):
        project = Project.objects.get(name='DummyTestProject')
        plan1 = TestPlan.objects.create(
            name='DummyTestPlan2', project=project)
        plan2 = TestPlan.objects.create(
            name='DummyTestPlan2', project=project)
        self._create_launch(plan1.id)
        self._create_launch(plan2.id)

        response = self._call_rest(
            'get',
            'launches/custom_list/?testplan_id__in={},{}'.format(
                plan1.id, plan2.id))
        self.assertEqual(len(response['results']), 2)

    def test_days_filter(self):
        test_plan = TestPlan.objects.get(name='DummyTestPlan')
        self._create_launch(test_plan.id)

        response = self._call_rest(
            'get',
            'launches/custom_list/?days=0')
        self.assertEqual(len(response['results']), 0)

        response = self._call_rest(
            'get',
            'launches/custom_list/?days=1')
        self.assertEqual(len(response['results']), 1)

    def test_from_to_filter(self):
        test_plan = TestPlan.objects.get(name='DummyTestPlan')
        self._create_launch(test_plan.id)
        today = datetime.today()

        response = self._call_rest(
            'get',
            'launches/custom_list/?from={}&to={}'.format(
                today.strftime('%Y-%m-%d'), today.strftime('%Y-%m-%d')))
        self.assertEqual(len(response['results']), 0)

        response = self._call_rest(
            'get',
            'launches/custom_list/?from={}&to={}'.format(
                today.strftime('%Y-%m-%d'),
                (today + timedelta(days=1)).strftime('%Y-%m-%d')))
        self.assertEqual(len(response['results']), 1)

    def test_build_in_filter(self):
        test_plan = TestPlan.objects.get(name='DummyTestPlan')
        launch1 = self._create_launch(test_plan.id)
        launch2 = self._create_launch(test_plan.id)
        launch3 = self._create_launch(test_plan.id)
        launch4 = self._create_launch(test_plan.id)

        Build(launch_id=launch1['id'], hash='c2').save()
        Build(launch_id=launch3['id'], hash='c3').save()
        Build(launch_id=launch4['id'], hash='c2').save()
        build = Build(launch_id=launch2['id'], hash='c1')
        build.set_last_commits(['c1', 'c2'])
        build.save()

        self.assertEqual(len(self.get_launches()['results']), 4)

        response = self._call_rest(
            'get', 'launches/custom_list/?build_hash__in=c1,c2')
        self.assertEqual(len(response['results']), 3)
        ids = []
        for res in response['results']:
            ids.append(res['id'])
        ids.sort()
        self.assertEqual(ids, [launch1['id'], launch2['id'], launch4['id']])

    def test_update_metrics(self):
        metrics = {'m1': 1, 'm2': 2}
        test_plan = TestPlan.objects.get(name='DummyTestPlan')
        launch = self._create_launch(test_plan.id)
        self.assertFalse(launch['parameters'])
        response = self._call_rest(
            'post', 'launches/{0}/update_metrics/'.format(launch['id']),
            {'metrics': metrics})

        self.assertTrue(response['parameters']['metrics'])
        self.assertEqual(metrics, response['parameters']['metrics'])

    def test_update_metrics_without_launch(self):
        metrics = {'m1': 1, 'm2': 2}
        response = self._call_rest(
            'post', 'launches/1/update_metrics/', {'metrics': metrics})
        self.assertEqual('Launch with id=1 does not exist',
                         response['message'])

    def test_update_metrics_without_metrics(self):
        test_plan = TestPlan.objects.get(name='DummyTestPlan')
        launch = self._create_launch(test_plan.id)
        response = self._call_rest(
            'post', 'launches/{0}/update_metrics/'.format(launch['id']),
            {'metrics': ''})
        self.assertEqual('No metrics in post request: '
                         '{}'.format({'metrics': ''}), response['message'])

        response = self._call_rest(
            'post', 'launches/{0}/update_metrics/'.format(launch['id']),
            {'commits': []})
        self.assertEqual('No metrics in post request: '
                         '{}'.format({'commits': []}), response['message'])

    def test_update_metrics_invalid(self):
        test_plan = TestPlan.objects.get(name='DummyTestPlan')
        launch = self._create_launch(test_plan.id)
        response = self._call_rest(
            'post', 'launches/{0}/update_metrics/'.format(launch['id']),
            {'metrics': 'blabla'})
        self.assertEqual('Invalid format for metrics \'blabla\', '
                         'expect object', response['message'])

        response = self._call_rest(
            'post', 'launches/{0}/update_metrics/'.format(launch['id']),
            {'metrics': [1, 2, 3]})
        self.assertEqual('Invalid format for metrics \'[1, 2, 3]\', '
                         'expect object', response['message'])

    def _get_testresult_data(self, launch_id, launch_item_id):
        return [{
            'launch': launch_id,
            'name': 'DummyTestCase',
            'suite': 'DummyTestSuite',
            'state': FAILED,
            'failure_reason': None,
            'duration': 1,
            'launch_item_id': launch_item_id
        }]

    def _create_testresult(self, data):
        return self._call_rest('post', 'testresults/', data)

    def test_result_count_filter(self):
        test_plan = TestPlan.objects.get(name='DummyTestPlan')
        launch = self._create_launch(test_plan.id)
        item1 = self._create_launch_item({
            'test_plan': test_plan.id,
            'command': 'sleep 600',
            'type': INIT_SCRIPT,
            'timeout': 1200,
        })
        item2 = self._create_launch_item({
            'test_plan': test_plan.id,
            'command': 'sleep 600',
            'type': ASYNC_CALL,
            'timeout': 1200,
        })

        data = self._get_testresult_data(launch['id'], item1['id'])
        self._create_testresult(data)
        data = self._get_testresult_data(launch['id'], item2['id'])
        self._create_testresult(data)
        data = self._get_testresult_data(launch['id'], item2['id'])
        self._create_testresult(data)

        response = self._call_rest(
            'get',
            'launches/custom_list/?results_group_count={}&state={}'.format(
                launch['id'], FAILED))

        self.assertEqual(response['results'],
                         [{'launch_item_id': item1['id'], 'count': 1},
                          {'launch_item_id': item2['id'], 'count': 2}])

        response = self._call_rest(
            'get',
            'launches/custom_list/?results_group_count={}&state={}'.format(
                launch['id'], PASSED))
        self.assertEqual(response['results'], [])


class TestResultApiTestCase(AbstractEntityApiTestCase):
    def setUp(self):
        project = Project.objects.create(name='DummyTestProject')
        self.test_plan = TestPlan.objects.create(name='DummyTestPlan',
                                                 project=project)
        self.launch = Launch.objects.create(test_plan=self.test_plan,
                                            started_by='http://2gis.local/')

    def _get_testresult_data(self, launch_id):
        return [{
            'launch': launch_id,
            'name': 'DummyTestCase',
            'suite': 'DummyTestSuite',
            'state': PASSED,
            'failure_reason': 'ololo',
            'duration': 1
        }, {
            'launch': launch_id,
            'name': 'SecondDummyTestSuite',
            'suite': 'SecondDummyTestSuite',
            'state': FAILED,
            'failure_reason': 'Exception: Clear message about failure',
            'duration': 5
        }]

    def _create_testresult(self, data):
        return self._call_rest('post', 'testresults/', data)

    def _get_testresults(self, filter=None):
        if filter is not None:
            return self._call_rest('get',
                                   'testresults/custom_list/?{}'.format(
                                       filter))
        else:
            return self._call_rest('get', 'testresults/')

    def test_creation(self):
        data = self._get_testresult_data(self.launch.id)
        self._create_testresult(data)
        self.assertEqual(len(self._get_testresults()['results']), 2)

    def test_filter_in(self):
        launches = [self.launch,
                    Launch.objects.create(test_plan=self.test_plan,
                                          started_by='http://2gis.local/')]
        data1 = self._get_testresult_data(launches[0].id)
        self._create_testresult(data1)
        data2 = self._get_testresult_data(launches[1].id)
        self._create_testresult(data2)
        url = 'launch_id__in={},{}&state={}'.format(
            launches[0].id, launches[1].id, FAILED)
        self.assertEqual(2, self._get_testresults(url)['count'])
        url = 'launch_id__in={},{}&state={}'.format(
            launches[0].id, launches[1].id, PASSED)
        self.assertEqual(2, self._get_testresults(url)['count'])
        url = 'launch_id__in={},{}&state__in={},{}'.format(
            launches[0].id, launches[1].id, PASSED, FAILED)
        self.assertEqual(4, self._get_testresults(url)['count'])

        self.assertEqual(4, self._get_testresults('launch_id__in=')['count'])
        self.assertEqual(4, self._get_testresults('state__in=')['count'])

    def test_clean_expired_results(self):
        data = self._get_testresult_data(self.launch.id)
        self._create_testresult(data)
        self.assertEqual(len(self._get_testresults()['results']), 2)
        self.launch.finished = timezone.now().date() - timedelta(days=31)
        self.launch.save()

        cleanup_database()
        self.assertEqual(len(self._get_testresults()['results']), 0)

    def test_not_clean_actual_results(self):
        data = self._get_testresult_data(self.launch.id)
        self._create_testresult(data)
        self.assertEqual(len(self._get_testresults()['results']), 2)
        self.launch.finished = timezone.now().date() - timedelta(days=1)
        self.launch.save()

        cleanup_database()
        self.assertEqual(len(self._get_testresults()['results']), 2)

    def test_history(self):
        project = Project.objects.get(name='DummyTestProject')
        testplan1 = self.test_plan
        testplan2 = TestPlan.objects.create(name='DummyTestPlan2',
                                            project=project)

        launches_tp1 = [self.launch,
                        Launch.objects.create(test_plan=testplan1,
                                              started_by='http://2gis.local/')]

        launch_tp2 = Launch.objects.create(test_plan=testplan2,
                                           started_by='http://2gis.local/')

        data11 = self._get_testresult_data(launches_tp1[0].id)
        data12 = self._get_testresult_data(launches_tp1[1].id)
        data21 = self._get_testresult_data(launch_tp2.id)
        self._create_testresult(data11)
        self._create_testresult(data12)
        self._create_testresult(data21)

        response = self._get_testresults('history=1')
        self.assertEqual(2, response['count'])

    def test_history_days(self):
        project = Project.objects.get(name='DummyTestProject')
        testplan1 = self.test_plan
        testplan2 = TestPlan.objects.create(name='DummyTestPlan2',
                                            project=project)

        launches_tp1 = [self.launch,
                        Launch.objects.create(test_plan=testplan1,
                                              started_by='http://2gis.local/')]

        launch_tp2 = Launch.objects.create(test_plan=testplan2,
                                           started_by='http://2gis.local/')

        data11 = self._get_testresult_data(launches_tp1[0].id)
        data12 = self._get_testresult_data(launches_tp1[1].id)
        data21 = self._get_testresult_data(launch_tp2.id)
        self._create_testresult(data11)
        self._create_testresult(data12)
        self._create_testresult(data21)

        response = self._get_testresults('history=1&days=0')
        self.assertEqual(0, response['count'])

    def test_search_positive(self):
        data = self._get_testresult_data(self.launch.id)
        self._create_testresult(data)
        self.assertEqual(len(self._get_testresults()['results']), 2)

        response = self._call_rest('get',
                                   'testresults/?search={}'.format('Second'))
        self.assertEqual(1, response['count'])
        self.assertEqual('SecondDummyTestSuite',
                         response['results'][0]['name'])

        response = self._call_rest('get',
                                   'testresults/?search={}'.format('Dummy'))
        self.assertEqual(2, response['count'])

    def test_search_negative(self):
        data = self._get_testresult_data(self.launch.id)
        self._create_testresult(data)
        self.assertEqual(len(self._get_testresults()['results']), 2)

        response = self._call_rest('get', 'testresults_negative/?search={}'.
                                   format('^((?!Exception).)*$'))
        self.assertEqual(1, response['count'])
        self.assertEqual('DummyTestCase',
                         response['results'][0]['name'])


class CommentsApiTestCase(AbstractEntityApiTestCase):
    comment = 'Dummy comment text'

    def _create_comment(self, comment):
        data = {
            'comment': comment,
            'content_type': 'user',
            'object_pk': 1
        }
        return self._call_rest('post', 'comments/', data)

    def _get_comments(self):
        return self._call_rest('get', 'comments/')

    def test_comment_create(self):
        output = self._create_comment(self.comment)
        self.assertEqual(output['comment'], self.comment)

        comments = self._get_comments()['results']
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]['comment'], self.comment)


@override_settings(
    BUG_TRACKING_SYSTEM_HOST='jira.local',
    BUG_TRACKING_SYSTEM_BUG_PATH='/rest/api/latest/issue/{issue_id}',
    BUG_STATE_EXPIRED=['Closed'])
class BugsApiTestCase(AbstractEntityApiTestCase):
    issue_found = '{"key": "ISSUE-1","fields": ' \
                  '{"status": {"name": "Closed"},"summary": "Issue Title"}}'
    issue_open_status = '{"key": "ISSUE-1","fields": ' \
                        '{"status": {"name": "Open"},' \
                        '"summary": "Issue Title"}}'
    issue_not_found = '{"errorMessages": ' \
                      '["Issue Does Not Exist"], "errors": { } }'
    issue_errors = '{"errorMessages": ' \
                   '[], "errors": {"project":"project is required"} }'

    def issue_request(self, externalId):
        return 'https://{}/rest/api/latest/issue/{}'.\
               format(settings.BUG_TRACKING_SYSTEM_HOST, externalId)

    def _create_bug(self, issue_name='ISSUE-1'):
        data = {
            'externalId': issue_name,
            'regexp': 'Regexp'
        }
        return self._call_rest('post', 'bugs/', data)

    def _create_bug_db(self, extId, regexp, state, name):
        Bug.objects.create(externalId=extId, regexp=regexp,
                           state=state, name=name)

    def _get_bugs(self):
        return self._call_rest('get', 'bugs/')

    @requests_mock.Mocker()
    def test_bug_create(self, m):
        m.get(self.issue_request('ISSUE-1'), text=self.issue_found)
        response = self._create_bug()
        self.assertEqual(201, response.status_code)

        response = self._get_bugs()
        self.assertEqual(1, len(response['results']))
        issue = response['results'][0]
        self.assertEqual('ISSUE-1', issue['externalId'])
        self.assertEqual('Closed', issue['status'])
        self.assertEqual('Issue Title', issue['name'])
        self.assertEqual('Regexp', issue['regexp'])

    @requests_mock.Mocker()
    def test_create_not_existent_bug(self, m):
        m.get(self.issue_request('ISSUE-1'), text=self.issue_not_found)
        response = self._create_bug()
        self.assertEqual('Issue Does Not Exist', response['message'])

    @requests_mock.Mocker()
    def test_create_errors_bug(self, m):
        m.get(self.issue_request('ISSUE-1'), text=self.issue_errors)
        response = self._create_bug()
        self.assertEqual('project', response['message'])

    @requests_mock.Mocker()
    @override_settings(TIME_BEFORE_UPDATE_BUG_INFO=0)
    def test_update_bug_not_exist(self, m):
        m.get(self.issue_request('ISSUE-1'), text=self.issue_found)
        m.get(self.issue_request('ISSUE-2'), text=self.issue_not_found)

        self._create_bug_db('ISSUE-1', 'regexp', 'Open', 'Issue Title')
        self._create_bug_db('ISSUE-2', 'regexp', 'Open', 'Issue Title')

        update_bugs()
        response = self._call_rest('get', 'bugs/1/')
        self.assertEqual('Closed', response['status'])
        response = self._call_rest('get', 'bugs/2/')
        self.assertEqual('Open', response['status'])

    @requests_mock.Mocker()
    def test_update_bug_recently(self, m):
        m.get(self.issue_request('ISSUE-1'), text=self.issue_found)
        self._create_bug_db('ISSUE-1', 'regexp', 'Open', 'Issue Title')

        update_bugs()
        response = self._call_rest('get', 'bugs/1/')
        self.assertEqual('Open', response['status'])

    @requests_mock.Mocker()
    def test_bug_released_change_status(self, m):
        m.get(self.issue_request('ISSUE-1'), text=self.issue_open_status)
        self._create_bug_db('ISSUE-1', 'regexp', 'Closed', 'Issue Title')

        update_bugs()
        response = self._call_rest('get', 'bugs/1/')
        self.assertEqual('Open', response['status'])

    @requests_mock.Mocker()
    def test_bug_not_expired(self, m):
        m.get(self.issue_request('ISSUE-1'), text=self.issue_found)
        self._create_bug_db('ISSUE-1', 'regexp', 'Closed', 'Issue Title')

        update_bugs()
        response = self._call_rest('get', 'bugs/1/')
        self.assertEqual('Closed', response['status'])

    @requests_mock.Mocker()
    @override_settings(BUG_TIME_EXPIRED=0)
    def test_bug_expired(self, m):
        m.get(self.issue_request('ISSUE-1'), text=self.issue_found)
        self._create_bug_db('ISSUE-1', 'regexp', 'Closed', 'Issue Title')

        update_bugs()
        response = self._get_bugs()
        self.assertEqual(0, len(response['results']))

    @requests_mock.Mocker()
    def test_bug_custom_list(self, m):
        m.get(self.issue_request('ISSUE-1'), text=self.issue_found)
        m.get(self.issue_request('ISSUE-2'), text=self.issue_found)
        m.get(self.issue_request('JIRA-1'), text=self.issue_found)
        m.get(self.issue_request('TEST-1'), text=self.issue_found)
        self._create_bug('ISSUE-1')
        self._create_bug('ISSUE-2')
        self._create_bug('JIRA-1')
        self._create_bug('TEST-1')

        # without filter
        response = self._get_bugs()
        self.assertEqual(4, len(response['results']))

        # empty filter
        response = self._call_rest(
            'get', 'bugs/custom_list/?issue_names__in=')
        self.assertEqual(4, len(response['results']))

        # two values in filter
        response = self._call_rest(
            'get', 'bugs/custom_list/?issue_names__in=ISSUE,JIRA')
        self.assertEqual(3, len(response['results']))

        # one value in filter
        response = self._call_rest(
            'get', 'bugs/custom_list/?issue_names__in=TEST')
        self.assertEqual(1, len(response['results']))
        issue = response['results'][0]
        self.assertEqual('TEST-1', issue['externalId'])


class StagesApiTestCase(AbstractEntityApiTestCase):
    def setUp(self):
        project = Project.objects.create(name='DummyTestProject')
        Stage.objects.create(name="DummyStage", project=project)
        super(StagesApiTestCase, self).setUp()

    def _get_stages(self):
        return self._call_rest('get', 'stages/')

    def test_get_stage(self):
        stages = self._get_stages()
        self.assertEqual(stages['count'], 1)
        self.assertEqual(len(stages['results']), 1)

    def test_patch_existing_stage(self):
        stage = self._get_stages()['results'][0]
        data = {'name': 'NewStageName', 'project': stage['project']}
        response = self._call_rest('patch',
                                   'stages/{0}/'.format(stage['id']), data)
        self.assertEqual('NewStageName', response['name'])
        stages = self._get_stages()['results']
        self.assertEqual(len(stages), 1)

    def test_patch_not_existing_stage(self):
        stage = self._get_stages()['results'][0]
        data = {'name': 'NewStageName', 'project': stage['project']}
        response = self._call_rest('patch', 'stages/100/', data)
        self.assertEqual('Not found.', response['detail'])


class ExternalApiTestCase(AbstractEntityApiTestCase):
    data_jenkins = {
        'name': 'DummyStage',
        'url': 'job/asgard/',
        'build': {
            'full_url': 'http://localhost:8080/job/asgard/18/',
            'phase': 'COMPLETED'
        }
    }

    data_rundeck = """<?xml version='1.0' encoding='utf8'?>
    <notification status="failed">
        <executions count="1">
            <execution id="1" href="http://localhost:8080/" status="failed">
                <job id="1" averageDuration="300">
                    <group>DummyStage</group>
                </job>
            </execution>
        </executions>
    </notification>"""

    def _get_stages(self):
        return self._call_rest('get', 'stages/')

    def test_not_supported_type(self):
        media_type = u'text/json'
        response = self._call_rest(
            'post', 'external/jenkins/ABC/',
            self.data_jenkins, content_type=media_type)
        self.assertEqual(
            'Unsupported media type "{0}" in request.'.format(media_type),
            json.loads(response.content.decode('utf-8',
                                               errors='replace'))['detail'])

    def test_jenkins_not_existing_project(self):
        response = self._call_rest(
            'post', 'external/jenkins/ABC/', self.data_jenkins)
        self.assertEqual('Project ABC does not exist',
                         response['message'])

    def test_jenkins_not_existing_stage(self):
        self.data_jenkins['build']['status'] = 'SUCCESS'
        project = Project.objects.create(name='DummyTestProject')
        self._call_rest(
            'post', 'external/jenkins/{0}/'.format(project.name),
            self.data_jenkins)
        stages = self._get_stages()['results']
        self.assertEqual(len(stages), 1)

        stage = stages[0]
        self.assertEqual('DummyStage', stage['name'])
        self.assertEqual(project.id, stage['project'])
        self.assertEqual('success', stage['state'])

    def test_jenkins_existing_stage(self):
        self.data_jenkins['build']['status'] = 'FAILURE'
        project = Project.objects.create(name='DummyTestProject')
        Stage.objects.create(name="DummyStage", project=project)
        self._call_rest(
            'post', 'external/jenkins/{0}/'.format(project.name),
            self.data_jenkins)
        stages = self._get_stages()['results']
        self.assertEqual(len(stages), 1)

        stage = stages[0]
        self.assertEqual('DummyStage', stage['name'])
        self.assertEqual(project.id, stage['project'])
        self.assertEqual('danger', stage['state'])

    def test_rundeck_not_existing_project(self):
        response = self._call_rest(
            'post', 'external/rundeck/ABC/',
            self.data_rundeck, content_type=u'text/xml')
        self.assertEqual(404, response.status_code)

    def test_rundeck_not_existing_stage(self):
        project = Project.objects.create(name='DummyTestProject')
        self._call_rest(
            'post', 'external/rundeck/{0}/'.format(project.name),
            self.data_rundeck, content_type=u'text/xml')
        stages = self._get_stages()['results']
        self.assertEqual(len(stages), 1)

        stage = stages[0]
        self.assertEqual('DummyStage', stage['name'])
        self.assertEqual(project.id, stage['project'])
        self.assertEqual('danger', stage['state'])

    def test_rundeck_existing_stage(self):
        data_rundeck = """<?xml version='1.0' encoding='utf8'?>
        <notification status="failed">
        <executions count="1">
        <execution id="1" href="http://localhost:8080/" status="succeeded">
        <job id="1" averageDuration="300">
        <group>DummyStage</group>
        </job>
        </execution>
        </executions>
        </notification>"""
        project = Project.objects.create(name='DummyTestProject')
        Stage.objects.create(name="DummyStage", project=project)
        self._call_rest(
            'post', 'external/rundeck/{0}/'.format(project.name),
            data_rundeck, content_type=u'text/xml')
        stages = self._get_stages()['results']
        self.assertEqual(len(stages), 1)

        stage = stages[0]
        self.assertEqual('DummyStage', stage['name'])
        self.assertEqual(project.id, stage['project'])
        self.assertEqual('success', stage['state'])


class MetricsApiTestCase(AbstractEntityApiTestCase):
    def setUp(self):
        self.project = Project.objects.create(name='TestProject')
        CrontabSchedule.objects.create(
            minute='*',
            hour='*',
            day_of_week='*',
            day_of_month='*',
            month_of_year='*'
        )
        super(MetricsApiTestCase, self).setUp()
        self._set_user_permissions()

    def _set_user_permissions(self):
        content_type = ContentType.objects.get_for_model(Metric)
        permissions = Permission.objects.filter(content_type=content_type)
        user = User.objects.get(username=self.user_login)
        for permission in permissions:
            user.user_permissions.add(permission)

    def _remove_user_permission(self, codename):
        user = User.objects.get(username=self.user_login)
        user.user_permissions.remove(
            Permission.objects.get(codename=codename))

    def _create_metric(self, project, schedule='* * * * *',
                       name='TestMetric', handler='count'):
        data = {
            'project': project.id,
            'name': name,
            'schedule': schedule,
            'handler': handler,
            'query': 'TestQuery',
            'weight': 1
        }
        return self._call_rest('post', 'metrics/', data)

    def test_create_new_metric_without_permissions(self):
        self._remove_user_permission('add_metric')
        response = self._create_metric(self.project)
        self.assertEqual('You do not have permission to perform this action.',
                         response['detail'])

    def test_create_new_metric_without_crontab(self):
        metric = self._create_metric(self.project, schedule='0 0 * * *')
        self.assertEqual(1, len(Metric.objects.all()))

        crontab = CrontabSchedule.objects.all()
        self.assertEqual(2, len(crontab))

        periodic_task = PeriodicTask.objects.all()
        self.assertEqual(1, len(periodic_task))
        self.assertEqual(
            'metrics.tasks.run_metric_calculation',
            periodic_task[0].task)
        self.assertEqual('[{}]'.format(metric['id']), periodic_task[0].args)
        self.assertEqual(True, periodic_task[0].enabled)

        self.assertEqual('TestMetric', metric['name'])
        self.assertEqual('TestQuery', metric['query'])
        self.assertEqual('count', metric['handler'])

    def test_create_new_metric_with_crontab(self):
        metric = self._create_metric(self.project)
        self.assertEqual('* * * * *', metric['schedule'])
        self.assertEqual(1, len(CrontabSchedule.objects.all()))
        self.assertEqual(1, len(Metric.objects.all()))

    def test_create_metric_incorrect_handler(self):
        response = self._create_metric(self.project, handler='asdzxc')
        self.assertEqual('Handler "asdzxc" is not a valid choice',
                         response['message'])

    def test_create_existing_metric(self):
        self._create_metric(self.project)
        response = self._create_metric(self.project)
        self.assertEqual('Metric already exist, choose another name',
                         response['message'])

    def test_create_metric_with_empty_name(self):
        response = self._create_metric(self.project, name='')
        self.assertEqual('Field "name" is required',
                         response['message'])

    def test_update_non_existent_metric(self):
        data = {
            'name': 'NewTestMetric',
            'schedule': '* * * * *',
            'handler': 'average',
            'query': 'NewTestQuery'
        }
        response = self._call_rest('patch', 'metrics/1/', data)
        self.assertEqual('Metric not found', response['message'])

    def test_update_name_for_metric(self):
        metric = self._create_metric(self.project)
        response = self._call_rest('patch',
                                   'metrics/{}/'.format(metric['id']),
                                   {'name': 'TestMetric'})
        self.assertEqual('TestMetric', response['name'])

    def test_update_metric_and_cron(self):
        metric = self._create_metric(self.project, schedule='0 0 * * *')

        data = {
            'name': 'NewTestMetric',
            'schedule': '* * * * *',
            'handler': 'cycletime',
            'query': 'NewTestQuery'
        }
        new_metric = self._call_rest('patch',
                                     'metrics/{}/'.format(metric['id']), data)

        new_periodic_task = PeriodicTask.objects.all()
        self.assertEquals(2, len(CrontabSchedule.objects.all()))
        self.assertEqual(1, len(new_periodic_task))
        self.assertEqual('NewTestMetric', new_metric['name'])
        self.assertEqual(new_periodic_task[0].crontab_id,
                         CrontabSchedule.objects.get(minute='*', hour='*').id)

    def test_update_only_metric(self):
        metric = self._create_metric(self.project)

        data = {
            'project': self.project.id,
            'name': 'NewTestMetric',
            'schedule': '* * * * *',
            'handler': 'cycletime',
            'query': 'NewTestQuery'
        }
        new_metric = self._call_rest('patch',
                                     'metrics/{}/'.format(metric['id']), data)

        new_periodic_task = PeriodicTask.objects.all()
        self.assertEquals(1, len(CrontabSchedule.objects.all()))
        self.assertEqual(1, len(new_periodic_task))
        self.assertEqual('NewTestMetric', new_metric['name'])
        self.assertEqual(new_periodic_task[0].crontab_id,
                         CrontabSchedule.objects.get(minute='*', hour='*').id)

    def test_update_metric_on_existent_name(self):
        self._create_metric(self.project)
        metric = self._create_metric(self.project, name='NewTestMetric')

        data = {'name': 'TestMetric'}
        response = self._call_rest('patch',
                                   'metrics/{}/'.format(metric['id']), data)
        self.assertEqual('Metric already exist, choose another name',
                         response['message'])

    def test_delete_metric(self):
        metric = self._create_metric(self.project)

        MetricValue.objects.create(metric_id=metric['id'], value='123')

        response = self._call_rest('delete',
                                   'metrics/{}/'.format(metric['id']))
        self.assertEqual('Metric and all values deleted', response['message'])

        self.assertEqual(0, len(Metric.objects.all()))
        self.assertEqual(0, len(MetricValue.objects.all()))
        self.assertEqual(0, len(PeriodicTask.objects.all()))
        self.assertEqual(1, len(CrontabSchedule.objects.all()))


@override_settings(S3_ACCESS_KEY=None, S3_SECRET_KEY=None, S3_HOST=None)
class ReportFileApiTestCase(AbstractEntityApiTestCase):
    def _post(self, file_name, url, data=None, auth=True):
        if auth:
            auth = '{}:{}'.format(self.user_login, self.user_plain_password)
            credentials = base64.b64encode(auth.encode('ascii'))
            self.client.defaults['HTTP_AUTHORIZATION'] =\
                'Basic ' + credentials.decode('utf-8')

        path = os.path.join(os.path.dirname(__file__),
                            'testdata/{}'.format(file_name))
        with open(path, 'rb') as fp:
            post_data = {'file': fp}
            if data is not None:
                post_data = {'file': fp}
                for key, val in data.items():
                    post_data[key] = val
            response = self.client.post('/{0}/external/report-xunit/{1}'.
                                        format(settings.CDWS_API_PATH, url),
                                        post_data)

        if response.content != b'':
            return (
                json.loads(response.content.decode('utf-8', errors='replace')),
                response.status_code)

    def test_upload_junit_file(self):
        project = Project.objects.create(name='DummyTestProject')
        testplan = TestPlan.objects.create(name='DummyTestPlan',
                                           project=project)
        self._post(file_name='junit-test-report.xml',
                   url='{}/junit/junit.xml'.format(testplan.id))

        launches = self._call_rest('get',
                                   'launches/?testplan={}'.format(testplan.id))
        self.assertEqual(1, launches['count'])
        launch = launches['results'][0]
        failed = self._call_rest(
            'get',
            'testresults/?launch={}&state={}'.format(launch['id'], FAILED))
        blocked = self._call_rest(
            'get',
            'testresults/?launch={}&state={}'.format(launch['id'], BLOCKED))
        skipped = self._call_rest(
            'get',
            'testresults/?launch={}&state={}'.format(launch['id'], SKIPPED))
        passed = self._call_rest(
            'get',
            'testresults/?launch={}&state={}'.format(launch['id'], PASSED))
        self.assertEqual(1, failed['count'])
        self.assertEqual(1, blocked['count'])
        self.assertEqual(
            'Failure message',
            failed['results'][0]['failure_reason'])
        self.assertEqual(
            'Error messageSystem-out',
            blocked['results'][0]['failure_reason'])
        self.assertEqual(1, skipped['count'])
        self.assertEqual(1, passed['count'])
        self.assertEqual(0.4, launch['duration'])

    def test_upload_junit_file_notime(self):
        project = Project.objects.create(name='DummyTestProject')
        testplan = TestPlan.objects.create(name='DummyTestPlan',
                                           project=project)
        self._post(file_name='junit-test-report-notime.xml',
                   url='{}/junit/junit.xml'.format(testplan.id))

        launches = self._call_rest('get',
                                   'launches/?testplan={}'.format(testplan.id))
        self.assertEqual(1, launches['count'])
        launch = launches['results'][0]
        self.assertEqual(0.0, launch['duration'])

    def test_upload_nunit_file(self):
        project = Project.objects.create(name='DummyTestProject')
        testplan = TestPlan.objects.create(name='DummyTestPlan',
                                           project=project)
        self._post(file_name='nunit-test-report.xml',
                   url='{}/nunit/nunit.xml'.format(testplan.id))

        launches = self._call_rest('get',
                                   'launches/?testplan={}'.format(testplan.id))
        self.assertEqual(1, launches['count'])
        launch = launches['results'][0]
        failed = self._call_rest(
            'get',
            'testresults/?launch={}&state={}'.format(launch['id'], FAILED))
        skipped = self._call_rest(
            'get',
            'testresults/?launch={}&state={}'.format(launch['id'], SKIPPED))
        passed = self._call_rest(
            'get',
            'testresults/?launch={}&state={}'.format(launch['id'], PASSED))
        self.assertEqual(1, failed['count'])
        self.assertEqual(1, skipped['count'])
        self.assertEqual(1, passed['count'])
        self.assertEqual(0.2, launch['duration'])

    def test_upload_empty_file(self):
        project = Project.objects.create(name='DummyTestProject')
        testplan = TestPlan.objects.create(name='DummyTestPlan',
                                           project=project)
        response, code = self._post(
            file_name='empty-test-report.xml',
            url='{}/nunit/nunit.xml'.format(testplan.id))
        launches = self._call_rest('get',
                                   'launches/?testplan={}'.format(testplan.id))
        self.assertEqual(1, launches['count'])
        launch = launches['results'][0]
        self.assertEqual(response, {'launch_id': launch['id']})

        comments = self._call_rest('get', 'comments/')
        self.assertEqual(1, comments['count'])
        comment = comments['results'][0]
        self.assertEqual(comment['comment'],
                         'During xml parsing the following '
                         'error is received: "no element found: '
                         'line 1, column 0"')
        self.assertEqual(comment['user_data']['username'], 'xml-parser')

    def test_upload_unknown_file(self):
        project = Project.objects.create(name='DummyTestProject')
        testplan = TestPlan.objects.create(name='DummyTestPlan',
                                           project=project)
        response, code = self._post(
            file_name='nunit-test-report.xml',
            url='{}/asdf/nunit.xml'.format(testplan.id))

        self.assertEqual('Unknown file format', response['message'])
        self.assertEqual(400, code)

    def test_upload_file_to_launch(self):
        project = Project.objects.create(name='DummyTestProject')
        testplan = TestPlan.objects.create(name='DummyTestPlan',
                                           project=project)
        launch = Launch.objects.create(test_plan=testplan)
        self._post(file_name='junit-test-report.xml',
                   data={'launch': launch.id},
                   url='{}/junit/junit.xml'.format(testplan.id))

        launches = self._call_rest('get',
                                   'launches/?testplan={}'.format(testplan.id))
        self.assertEqual(1, launches['count'])
        launch = launches['results'][0]
        failed = self._call_rest(
            'get',
            'testresults/?launch={}&state={}'.format(launch['id'], FAILED))
        blocked = self._call_rest(
            'get',
            'testresults/?launch={}&state={}'.format(launch['id'], BLOCKED))
        skipped = self._call_rest(
            'get',
            'testresults/?launch={}&state={}'.format(launch['id'], SKIPPED))
        passed = self._call_rest(
            'get',
            'testresults/?launch={}&state={}'.format(launch['id'], PASSED))
        self.assertEqual(1, failed['count'])
        self.assertEqual(1, blocked['count'])
        self.assertEqual(1, skipped['count'])
        self.assertEqual(1, passed['count'])
        self.assertEqual(0.4, launch['duration'])

    def test_additional_information(self):
        data = \
            '{"env": {"BRANCH": "master"}, "options": {"started_by": "user",' \
            '"duration": "120.20"}}'
        project = Project.objects.create(name='DummyTestProject')
        testplan = TestPlan.objects.create(name='DummyTestPlan',
                                           project=project)
        launch = Launch.objects.create(test_plan=testplan)
        self._post(file_name='junit-test-report.xml',
                   data={'launch': launch.id, 'data': data},
                   url='{}/junit/junit.xml'.format(testplan.id))

        launches = self._call_rest('get',
                                   'launches/?testplan={}'.format(testplan.id))
        self.assertEqual(1, launches['count'])
        launch = launches['results'][0]
        self.assertEqual(json.loads(data), launch['parameters'])
        self.assertEqual('user', launch['started_by'])
        self.assertTrue(launch['build'])
        self.assertFalse(launch['build']['version'])
        self.assertFalse(launch['build']['hash'])
        self.assertFalse(launch['build']['branch'])
        self.assertEqual(120.2, launch['duration'])

    def test_additional_information_to_existent_launch(self):
        data = \
            '{"env": {"BRANCH": "master"}, "options": {"started_by": "user",' \
            '"duration": "120.20"}}'
        project = Project.objects.create(name='DummyTestProject')
        testplan = TestPlan.objects.create(name='DummyTestPlan',
                                           project=project)
        launch = Launch.objects.create(test_plan=testplan)
        self._post(file_name='junit-test-report.xml',
                   data={'launch': launch.id, 'data': data},
                   url='{}/junit/junit.xml'.format(testplan.id))
        self._post(file_name='junit-test-report.xml',
                   data={'launch': launch.id, 'data': data},
                   url='{}/junit/junit.xml'.format(testplan.id))

        launches = self._call_rest('get',
                                   'launches/?testplan={}'.format(testplan.id))
        self.assertEqual(1, launches['count'])
        launch = launches['results'][0]
        self.assertEqual(json.loads(data), launch['parameters'])
        self.assertEqual('user', launch['started_by'])
        self.assertTrue(launch['build'])
        self.assertFalse(launch['build']['version'])
        self.assertFalse(launch['build']['hash'])
        self.assertFalse(launch['build']['branch'])
        self.assertEqual(120.2, launch['duration'])

        failed = self._call_rest(
            'get',
            'testresults/?launch={}&state={}'.format(launch['id'], FAILED))
        blocked = self._call_rest(
            'get',
            'testresults/?launch={}&state={}'.format(launch['id'], BLOCKED))
        self.assertEqual(2, failed['count'])
        self.assertEqual(2, blocked['count'])

    def test_empty_started_by(self):
        data = '{"env": {"BRANCH": "master"}}'
        project = Project.objects.create(name='DummyTestProject')
        testplan = TestPlan.objects.create(name='DummyTestPlan',
                                           project=project)
        launch = Launch.objects.create(test_plan=testplan)
        self._post(file_name='junit-test-report.xml',
                   data={'launch': launch.id, 'data': data},
                   url='{}/junit/junit.xml'.format(testplan.id))

        launches = self._call_rest('get',
                                   'launches/?testplan={}'.format(testplan.id))
        self.assertEqual(1, launches['count'])
        launch = launches['results'][0]
        self.assertEqual(json.loads(data), launch['parameters'])
        self.assertFalse(launch['started_by'])

    def test_empty_add_info(self):
        data = ''
        project = Project.objects.create(name='DummyTestProject')
        testplan = TestPlan.objects.create(name='DummyTestPlan',
                                           project=project)
        launch = Launch.objects.create(test_plan=testplan)
        self._post(file_name='junit-test-report.xml',
                   data={'launch': launch.id, 'data': data},
                   url='{}/junit/junit.xml'.format(testplan.id))

        launches = self._call_rest('get',
                                   'launches/?testplan={}'.format(testplan.id))
        self.assertEqual(1, launches['count'])
        launch = launches['results'][0]
        self.assertFalse(launch['parameters'])
        self.assertFalse(launch['started_by'])

    def test_hash_from_last_commits(self):
        data = \
            '{"env": {"BRANCH": "master"}, "options": {"started_by": "user",' \
            '"last_commits": ["c1","c2","c3"]}}'
        project = Project.objects.create(name='DummyTestProject')
        testplan = TestPlan.objects.create(name='DummyTestPlan',
                                           project=project)
        launch = Launch.objects.create(test_plan=testplan)
        self._post(file_name='junit-test-report.xml',
                   data={'launch': launch.id, 'data': data},
                   url='{}/junit/junit.xml'.format(testplan.id))

        launches = self._call_rest('get', 'launches/')
        self.assertEqual(1, launches['count'])
        launch = launches['results'][0]
        self.assertTrue(launch['build'])
        self.assertEqual(launch['build']['hash'], 'c1')

    @override_settings(LAST_COMMITS_SIZE=2)
    def test_last_commits_limit(self):
        data = \
            '{"env": {"BRANCH": "master"}, "options": {"started_by": "user",' \
            '"last_commits": ["c1","c2","c3"]}}'
        project = Project.objects.create(name='DummyTestProject')
        testplan = TestPlan.objects.create(name='DummyTestPlan',
                                           project=project)
        launch = Launch.objects.create(test_plan=testplan)
        self._post(file_name='junit-test-report.xml',
                   data={'launch': launch.id, 'data': data},
                   url='{}/junit/junit.xml'.format(testplan.id))

        launches = self._call_rest('get', 'launches/')
        self.assertEqual(1, launches['count'])
        launch = launches['results'][0]
        self.assertTrue(launch['build'])
        self.assertEqual(launch['build']['last_commits'], ['c1', 'c2'])

    def test_upload_file_unauthorized(self):
        project = Project.objects.create(name='DummyTestProject')
        testplan = TestPlan.objects.create(name='DummyTestPlan',
                                           project=project)
        response, code = self._post(
            file_name='junit-test-report.xml',
            url='{}/junit/junit.xml'.format(testplan.id), auth=False)
        self.assertEqual(401, code)
