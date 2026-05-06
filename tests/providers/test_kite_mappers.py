"""Unit tests for Kite XML mappers."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from app.providers.kite.mappers import (
    parse_presence_fields,
    parse_subscription,
    parse_usage_snapshot,
)
from app.subscriptions.domain import AdministrativeStatus, ConnectivityState

_KITE_NS = "http://www.telefonica.com/schemas/UNICA/SOAP/Globalm2m/inventory/v12/types"


def _subscription_xml() -> ET.Element:
    xml = f"""
    <gm2minve_s3t:subscriptionDetailData xmlns:gm2minve_s3t="{_KITE_NS}">
      <gm2minve_s3t:subscriptionId>sub-123</gm2minve_s3t:subscriptionId>
      <gm2minve_s3t:icc>8934070100000000001</gm2minve_s3t:icc>
      <gm2minve_s3t:imsi>214070000000001</gm2minve_s3t:imsi>
      <gm2minve_s3t:msisdn>346000000001</gm2minve_s3t:msisdn>
      <gm2minve_s3t:alias>Tracker SIM</gm2minve_s3t:alias>
      <gm2minve_s3t:customField1>alpha</gm2minve_s3t:customField1>
      <gm2minve_s3t:customField2>beta</gm2minve_s3t:customField2>
      <gm2minve_s3t:simModel>SIM-XYZ</gm2minve_s3t:simModel>
      <gm2minve_s3t:simType>eSIM</gm2minve_s3t:simType>
      <gm2minve_s3t:imei>359000000000001</gm2minve_s3t:imei>
      <gm2minve_s3t:provisionDate>2024-08-01T00:00:00Z</gm2minve_s3t:provisionDate>
      <gm2minve_s3t:activationDate>2024-08-02T00:00:00Z</gm2minve_s3t:activationDate>
      <gm2minve_s3t:commercialGroup>IoT Base</gm2minve_s3t:commercialGroup>
      <gm2minve_s3t:supervisionGroup>Group-A</gm2minve_s3t:supervisionGroup>
      <gm2minve_s3t:billingAccount>BA-1</gm2minve_s3t:billingAccount>
      <gm2minve_s3t:staticIp>10.0.0.10</gm2minve_s3t:staticIp>
      <gm2minve_s3t:apn>iot.apn</gm2minve_s3t:apn>
      <gm2minve_s3t:apn0>iot.apn</gm2minve_s3t:apn0>
      <gm2minve_s3t:apn1>backup.apn</gm2minve_s3t:apn1>
      <gm2minve_s3t:staticIpAddress0>10.0.0.11</gm2minve_s3t:staticIpAddress0>
      <gm2minve_s3t:additionalStaticIpAddress0>10.0.0.12</gm2minve_s3t:additionalStaticIpAddress0>
      <gm2minve_s3t:orderNumber>ORD-9</gm2minve_s3t:orderNumber>
      <gm2minve_s3t:masterId>M-1</gm2minve_s3t:masterId>
      <gm2minve_s3t:masterName>Main Master</gm2minve_s3t:masterName>
      <gm2minve_s3t:serviceProviderEnablerId>SPE-1</gm2minve_s3t:serviceProviderEnablerId>
      <gm2minve_s3t:serviceProviderCommercialName>Telefónica</gm2minve_s3t:serviceProviderCommercialName>
      <gm2minve_s3t:customerID>C-1</gm2minve_s3t:customerID>
      <gm2minve_s3t:customerName>Customer One</gm2minve_s3t:customerName>
      <gm2minve_s3t:lifeCycleStatus>ACTIVE</gm2minve_s3t:lifeCycleStatus>
      <gm2minve_s3t:country>ES</gm2minve_s3t:country>
      <gm2minve_s3t:operator>Movistar</gm2minve_s3t:operator>
      <gm2minve_s3t:manualLocation>
        <gm2minve_s3t:coordinates>
          <gm2minve_s3t:latitude>40.4168</gm2minve_s3t:latitude>
          <gm2minve_s3t:longitude>-3.7038</gm2minve_s3t:longitude>
        </gm2minve_s3t:coordinates>
      </gm2minve_s3t:manualLocation>
      <gm2minve_s3t:automaticLocation>
        <gm2minve_s3t:coordinates>
          <gm2minve_s3t:latitude>41.0</gm2minve_s3t:latitude>
          <gm2minve_s3t:longitude>-4.0</gm2minve_s3t:longitude>
        </gm2minve_s3t:coordinates>
      </gm2minve_s3t:automaticLocation>
      <gm2minve_s3t:basicServices>
        <gm2minve_s3t:voiceOriginatedHome>true</gm2minve_s3t:voiceOriginatedHome>
        <gm2minve_s3t:voiceOriginatedRoaming>false</gm2minve_s3t:voiceOriginatedRoaming>
        <gm2minve_s3t:dataHome>true</gm2minve_s3t:dataHome>
      </gm2minve_s3t:basicServices>
      <gm2minve_s3t:supplServices>
        <gm2minve_s3t:vpn>true</gm2minve_s3t:vpn>
        <gm2minve_s3t:dim>false</gm2minve_s3t:dim>
      </gm2minve_s3t:supplServices>
      <gm2minve_s3t:sgsnIP>192.0.2.10</gm2minve_s3t:sgsnIP>
      <gm2minve_s3t:ggsnIP>192.0.2.11</gm2minve_s3t:ggsnIP>
      <gm2minve_s3t:commModuleManufacturer>Quectel</gm2minve_s3t:commModuleManufacturer>
      <gm2minve_s3t:commModuleModel>BG95</gm2minve_s3t:commModuleModel>
      <gm2minve_s3t:IMEILastChange>2024-08-03T00:00:00Z</gm2minve_s3t:IMEILastChange>
      <gm2minve_s3t:billingAccountName>Primary BA</gm2minve_s3t:billingAccountName>
      <gm2minve_s3t:servicePack>Pack-A</gm2minve_s3t:servicePack>
      <gm2minve_s3t:servicePackId>SP-1</gm2minve_s3t:servicePackId>
      <gm2minve_s3t:ip>10.0.0.20</gm2minve_s3t:ip>
      <gm2minve_s3t:additionalIp>10.0.0.21</gm2minve_s3t:additionalIp>
      <gm2minve_s3t:lteEnabled>true</gm2minve_s3t:lteEnabled>
      <gm2minve_s3t:eid>EID-1</gm2minve_s3t:eid>
      <gm2minve_s3t:swapStatus>ENABLED</gm2minve_s3t:swapStatus>
      <gm2minve_s3t:subscriptionType>UICC</gm2minve_s3t:subscriptionType>
      <gm2minve_s3t:ratType>7</gm2minve_s3t:ratType>
      <gm2minve_s3t:qci>9</gm2minve_s3t:qci>
      <gm2minve_s3t:lastStateChangeDate>2024-08-04T00:00:00Z</gm2minve_s3t:lastStateChangeDate>
      <gm2minve_s3t:lastTrafficDate>2024-08-05T00:00:00Z</gm2minve_s3t:lastTrafficDate>
      <gm2minve_s3t:blockReason1>none</gm2minve_s3t:blockReason1>
      <gm2minve_s3t:suspensionNextDate>2024-09-01T00:00:00Z</gm2minve_s3t:suspensionNextDate>
      <gm2minve_s3t:voLteEnabled>false</gm2minve_s3t:voLteEnabled>
      <gm2minve_s3t:defaultApn>iot.apn</gm2minve_s3t:defaultApn>
      <gm2minve_s3t:consumptionDaily>
        <gm2minve_s3t:voice>
          <gm2minve_s3t:limit>100</gm2minve_s3t:limit>
          <gm2minve_s3t:value>12</gm2minve_s3t:value>
          <gm2minve_s3t:thrReached>0</gm2minve_s3t:thrReached>
          <gm2minve_s3t:enabled>true</gm2minve_s3t:enabled>
          <gm2minve_s3t:trafficCut>false</gm2minve_s3t:trafficCut>
        </gm2minve_s3t:voice>
        <gm2minve_s3t:sms>
          <gm2minve_s3t:limit>50</gm2minve_s3t:limit>
          <gm2minve_s3t:value>5</gm2minve_s3t:value>
          <gm2minve_s3t:thrReached>0</gm2minve_s3t:thrReached>
          <gm2minve_s3t:enabled>true</gm2minve_s3t:enabled>
          <gm2minve_s3t:trafficCut>false</gm2minve_s3t:trafficCut>
        </gm2minve_s3t:sms>
        <gm2minve_s3t:data>
          <gm2minve_s3t:limit>1000</gm2minve_s3t:limit>
          <gm2minve_s3t:value>256</gm2minve_s3t:value>
          <gm2minve_s3t:thrReached>0</gm2minve_s3t:thrReached>
          <gm2minve_s3t:enabled>true</gm2minve_s3t:enabled>
          <gm2minve_s3t:trafficCut>false</gm2minve_s3t:trafficCut>
        </gm2minve_s3t:data>
      </gm2minve_s3t:consumptionDaily>
      <gm2minve_s3t:consumptionMonthly>
        <gm2minve_s3t:voice>
          <gm2minve_s3t:limit>1000</gm2minve_s3t:limit>
          <gm2minve_s3t:value>120</gm2minve_s3t:value>
          <gm2minve_s3t:thrReached>0</gm2minve_s3t:thrReached>
          <gm2minve_s3t:enabled>true</gm2minve_s3t:enabled>
          <gm2minve_s3t:trafficCut>false</gm2minve_s3t:trafficCut>
        </gm2minve_s3t:voice>
        <gm2minve_s3t:sms>
          <gm2minve_s3t:limit>500</gm2minve_s3t:limit>
          <gm2minve_s3t:value>45</gm2minve_s3t:value>
          <gm2minve_s3t:thrReached>0</gm2minve_s3t:thrReached>
          <gm2minve_s3t:enabled>true</gm2minve_s3t:enabled>
          <gm2minve_s3t:trafficCut>false</gm2minve_s3t:trafficCut>
        </gm2minve_s3t:sms>
        <gm2minve_s3t:data>
          <gm2minve_s3t:limit>2048</gm2minve_s3t:limit>
          <gm2minve_s3t:value>768</gm2minve_s3t:value>
          <gm2minve_s3t:thrReached>0</gm2minve_s3t:thrReached>
          <gm2minve_s3t:enabled>true</gm2minve_s3t:enabled>
          <gm2minve_s3t:trafficCut>false</gm2minve_s3t:trafficCut>
        </gm2minve_s3t:data>
      </gm2minve_s3t:consumptionMonthly>
    </gm2minve_s3t:subscriptionDetailData>
    """
    return ET.fromstring(xml)


def _presence_xml() -> ET.Element:
    xml = f"""
    <gm2minve_s3t:presenceDetailData xmlns:gm2minve_s3t="{_KITE_NS}">
      <gm2minve_s3t:level>IP</gm2minve_s3t:level>
      <gm2minve_s3t:timeStamp>2026-04-29T12:34:56Z</gm2minve_s3t:timeStamp>
      <gm2minve_s3t:cause>manual</gm2minve_s3t:cause>
      <gm2minve_s3t:ip>10.1.2.3</gm2minve_s3t:ip>
      <gm2minve_s3t:apn>iot.apn</gm2minve_s3t:apn>
      <gm2minve_s3t:ratType>7</gm2minve_s3t:ratType>
    </gm2minve_s3t:presenceDetailData>
    """
    return ET.fromstring(xml)


class TestKiteMapperSubscription:
    def test_subscription_maps_fields_and_contract(self) -> None:
        sub = parse_subscription(_subscription_xml(), "8934070100000000001", "company-1")

        assert sub.iccid == "8934070100000000001"
        assert sub.msisdn == "346000000001"
        assert sub.imsi == "214070000000001"
        assert sub.provider == "kite"
        assert sub.status == AdministrativeStatus.ACTIVE
        assert sub.native_status == "ACTIVE"
        assert sub.provider_fields["sgsn_ip"] == "192.0.2.10"
        assert sub.provider_fields["ggsn_ip"] == "192.0.2.11"
        assert sub.provider_fields["manual_location"] == {"lat": "40.4168", "lng": "-3.7038"}
        assert sub.provider_fields["automatic_location"] == {"lat": "41.0", "lng": "-4.0"}
        assert sub.provider_fields["basic_services"]["voiceOriginatedHome"] is True
        assert sub.provider_fields["supplementary_services"] == ["vpn"]
        assert sub.provider_fields["consumption_monthly"]["data"]["value"] == "768"
        assert sub.activated_at is not None
        assert sub.updated_at is not None


class TestKiteMapperUsage:
    def test_usage_maps_snapshot(self) -> None:
        snapshot = parse_usage_snapshot(_subscription_xml(), "8934070100000000001")

        assert snapshot.iccid == "8934070100000000001"
        assert snapshot.data_used_bytes == 768
        assert snapshot.sms_count == 45
        assert snapshot.voice_seconds == 120
        assert snapshot.provider_metrics["consumption_monthly"]["data"]["value"] == "768"
        assert len(snapshot.usage_metrics) == 6


class TestKiteMapperPresence:
    def test_presence_maps_state_and_timestamp(self) -> None:
        presence = parse_presence_fields(_presence_xml(), "8934070100000000001")

        assert presence.iccid == "8934070100000000001"
        assert presence.state == ConnectivityState.ONLINE
        assert presence.ip_address == "10.1.2.3"
        assert presence.rat_type == "7"
        assert presence.last_seen_at is not None
