import logging

from django.urls import reverse

from eth_account import Account
from ethereum.utils import check_checksum
from faker import Faker
from rest_framework import status
from rest_framework.test import APITestCase

from gnosis.eth.constants import NULL_ADDRESS
from gnosis.eth.utils import get_eth_address_with_invalid_checksum
from gnosis.safe.tests.utils import generate_salt_nonce

from safe_relay_service.tokens.tests.factories import TokenFactory

from ..models import SafeContract, SafeCreation2
from ..services.safe_creation_service import SafeCreationServiceProvider
from .relay_test_case import RelayTestCaseMixin

faker = Faker()

logger = logging.getLogger(__name__)


class TestViewsV2(APITestCase, RelayTestCaseMixin):
    @classmethod
    def setUpTestData(cls):
        cls.prepare_tests()

    def test_safe_creation(self):
        salt_nonce = generate_salt_nonce()
        owners = [Account.create().address for _ in range(2)]
        data = {
            'saltNonce': salt_nonce,
            'owners': owners,
            'threshold': len(owners)
        }
        response = self.client.post(reverse('v2:safe-creation'), data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        response_json = response.json()
        safe_address = response_json['safe']
        self.assertTrue(check_checksum(safe_address))
        self.assertTrue(check_checksum(response_json['paymentReceiver']))
        self.assertEqual(response_json['paymentToken'], NULL_ADDRESS)
        self.assertEqual(response_json['payment'], response_json['gasEstimated'] * response_json['gasPriceEstimated'])
        self.assertGreater(response_json['gasEstimated'], 0)
        self.assertGreater(response_json['gasPriceEstimated'], 0)
        self.assertGreater(len(response_json['setupData']), 2)

        self.assertTrue(SafeContract.objects.filter(address=safe_address))
        self.assertTrue(SafeCreation2.objects.filter(owners__contains=[owners[0]]))
        safe_creation = SafeCreation2.objects.get(safe=safe_address)
        self.assertEqual(safe_creation.payment_token, None)
        # Payment includes deployment gas + gas to send eth to the deployer
        self.assertEqual(safe_creation.payment, safe_creation.wei_estimated_deploy_cost())

        data = {
            'salt_nonce': -1,
            'owners': owners,
            'threshold': 2
        }
        response = self.client.post(reverse('v2:safe-creation'), data, format='json')
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

    def test_safe_creation_with_fixed_cost(self):
        salt_nonce = generate_salt_nonce()
        owners = [Account.create().address for _ in range(2)]
        data = {
            'saltNonce': salt_nonce,
            'owners': owners,
            'threshold': len(owners)
        }

        fixed_creation_cost = 123
        with self.settings(SAFE_FIXED_CREATION_COST=fixed_creation_cost):
            SafeCreationServiceProvider.del_singleton()
            response = self.client.post(reverse('v2:safe-creation'), data, format='json')
            SafeCreationServiceProvider.del_singleton()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        response_json = response.json()
        safe_address = response_json['safe']
        self.assertTrue(check_checksum(safe_address))
        self.assertTrue(check_checksum(response_json['paymentReceiver']))
        self.assertEqual(response_json['paymentToken'], NULL_ADDRESS)
        self.assertEqual(response_json['payment'], 123)
        self.assertGreater(response_json['gasEstimated'], 0)
        self.assertGreater(response_json['gasPriceEstimated'], 0)
        self.assertGreater(len(response_json['setupData']), 2)

    def test_safe_creation_with_payment_token(self):
        salt_nonce = generate_salt_nonce()
        owners = [Account.create().address for _ in range(2)]
        payment_token = Account.create().address
        data = {
            'saltNonce': salt_nonce,
            'owners': owners,
            'threshold': len(owners),
            'paymentToken': payment_token,
        }

        response = self.client.post(reverse('v2:safe-creation'), data=data, format='json')
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        response_json = response.json()
        self.assertIn('InvalidPaymentToken', response_json['exception'])
        self.assertIn(payment_token, response_json['exception'])

        fixed_eth_conversion = 0.1
        token_model = TokenFactory(address=payment_token, fixed_eth_conversion=fixed_eth_conversion)
        response = self.client.post(reverse('v2:safe-creation'), data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        response_json = response.json()
        safe_address = response_json['safe']
        self.assertTrue(check_checksum(safe_address))
        self.assertTrue(check_checksum(response_json['paymentReceiver']))
        self.assertEqual(response_json['paymentToken'], payment_token)
        self.assertEqual(response_json['payment'],
                         response_json['gasEstimated'] * response_json['gasPriceEstimated'] *
                         (1 / fixed_eth_conversion))
        self.assertGreater(response_json['gasEstimated'], 0)
        self.assertGreater(response_json['gasPriceEstimated'], 0)
        self.assertGreater(len(response_json['setupData']), 2)

        self.assertTrue(SafeContract.objects.filter(address=safe_address))
        self.assertTrue(SafeCreation2.objects.filter(owners__contains=[owners[0]]))
        safe_creation = SafeCreation2.objects.get(safe=safe_address)
        self.assertEqual(safe_creation.payment_token, payment_token)
        # Payment includes deployment gas + gas to send eth to the deployer
        self.assertEqual(safe_creation.payment, safe_creation.wei_estimated_deploy_cost() * (1 / fixed_eth_conversion))

    def test_safe_signal_v2(self):
        safe_address = Account.create().address

        response = self.client.get(reverse('v2:safe-signal', args=(safe_address,)))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        response = self.client.put(reverse('v2:safe-signal', args=(safe_address,)))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        invalid_address = get_eth_address_with_invalid_checksum()

        response = self.client.get(reverse('v2:safe-signal', args=(invalid_address,)))
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

        # We need ether or task will be hanged because of problems with retries emulating celery tasks during testing
        safe_creation2 = self.create2_test_safe_in_db()
        self.assertIsNone(safe_creation2.tx_hash)
        self.assertIsNone(safe_creation2.block_number)
        my_safe_address = safe_creation2.safe.address

        response = self.client.get(reverse('v2:safe-signal', args=(my_safe_address,)))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.json()['txHash'])
        self.assertIsNone(response.json()['blockNumber'])

        self.send_ether(my_safe_address, safe_creation2.payment)
        response = self.client.put(reverse('v2:safe-signal', args=(my_safe_address,)))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertTrue(self.ethereum_service.is_contract(my_safe_address))
        safe_creation2.refresh_from_db()
        self.assertIsNotNone(safe_creation2.tx_hash)

        response = self.client.get(reverse('v2:safe-signal', args=(my_safe_address,)))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()['txHash'], safe_creation2.tx_hash)
        self.assertEqual(response.json()['blockNumber'], safe_creation2.block_number)