"""Orders processing"""

import os
import json
import time
import calendar
import logging

from datetime import datetime
from flask import Blueprint, Response, request
from mongoengine import *
from hashids import Hashids

import requests
import requests_toolbelt.adapters.appengine

from ..my_models.my_rates import Currency, Rates
from ..my_models.my_item import Item, ShippingInfo
from ..my_models.my_order import Order
from ..my_models.my_user import User
from ..utils.shipping.amazon import AmazonShippingInfo
from ..utils import json

orders = Blueprint("orders", __name__)
endpoint = os.environ.get("HOSTGATOR_SYNC_ENDPOINT")
requests_toolbelt.adapters.appengine.monkeypatch()

def store_items_and_create_order(new_order, usd_to_kes):
    items = [Item(**item) for item in new_order["items"]]
    items_key = .put_multi(items)

    new_order["items"] = item_keys
    order = Order(**new_order)

    hashids = Hashids(salt="https://vitumob.com/orders", min_length=8)
    order.key = Key(order, hashids.encode("VM", str(calendar.timegm(time.gmtime()))))
    order.put()

    return {
        'order_id': order.key.id(),
        'order_hex': order.key.urlsafe(),
        'total_cost': order.total_cost,
        'customs': order.customs,
        'vat': order.vat,
        'overall_cost': order.overall_cost,
        'shipping_cost': order.shipping_cost,
        'markup': order.markup,
        'exchange_rate': usd_to_kes.rate,
    }
@orders.route("/order", methods=["POST"])
def new_order_from_extension():
    """Receives a new order and store it"""
    new_order = json.loads(request.json["order"])

    if os.environ.get("ENV") == "development":
        rates_key = Key(Rates, os.environ.get("OPENEXCHANGE_API_ID"))
        rates = Rates.get_by_id(rates_key.id())
        usd_to_kes = [rate for rate in rates.rates if rate.code == "KES"][0]
    else:
        usd_to_kes = Currency(code="KES", rate=105.00)

    if "amazon" in new_order["merchant"]:
        amazon = AmazonShippingInfo(new_order["items"])
        response, status_code = amazon.get_shipping_info()

        if len(response) == 0 and status_code != 200:
            return Response(json.dumps({"error" : response}), status=504, mimetype="application/json")

        items_with_shipping_info = response
        for index, item in enumerate(new_order["items"]):
            shipping_info = [info for info in items_with_shipping_info
                                if "asin" in info and info["asin"] == item["id"]]
            if len(shipping_info) == 0:
                logging.debug(
                    "No shipping information was captured for %s",
                    itme["name"]
                )
                new_order["items"][index] = item
                continue

            shipping_info = shipping_info[0]
            shipping_info["local_cost"] = shipping_info["shipping_cost"] * usd_to_kes.rate

            item["name"] = shipping_info["title"]
            item["shipping_cost"] = shipping_info["shipping_cost"] * item["quantity"]

            shipping_info.pop("asin", None)
            shipping_info.pop("title", None)

            item["shipping_info"] = ShippingInfo(**shipping_info)
            new_order["items"][index] = item

    def update_item_information(item):
        """delete id, calculate total_cost and add missing shipping_cost"""

        item["item_id"] = item["id"]
        item.pop("id", None)

        # get the item's price in KES
        item["local_price"] = round(item["price"] * usd_to_kes.rate, 2)

        # get total cost per item
        item["total_cost"] = item["price"] * item["quantity"]

        if "shipping_info" not in item:
            item["shipping_info"] = item["quantity"] * (2.20462 * 7.50)

        return item
    new_order["items"] = map(update_item_information, new_order["items"])
    new_order["exchange_rate"] = usd_to_kes.rate

    # calculate the order's total shipping cost'
    item_shipping_costs = [item["shipping_cost"] for item in new_order["items"]]
    new_order["shipping_cost"] = reduce(lambda a,b: a + b, item_shipping_costs, 0.00)

    # calculate the order's total item costs
    cost_per_items = [item["total_cost"] for item in new_order["items"]]
    new_order["total_cost"] = reduce(lambda a, b: a + b, cost_per_items, 0.00)

    new_order['total_cost'] = round(new_order['total_cost'], 2)
    new_order['shipping_cost'] = round(new_order['shipping_cost'], 2)

    # If the total cost of items is more than
    # $800 shipping cost is completely waved
    if new_order['total_cost'] >= 800:
        new_order['waived_shipping_cost'] = new_order['shipping_cost']
        new_order['shipping_cost'] = 0.00

    new_order['customs'] = round(new_order['total_cost'] * 0.12, 2)
    new_order['vat'] = round(new_order['total_cost'] * 0.16, 2)

    new_order['overall_cost'] = reduce(lambda a, b: a + b, [
        new_order['total_cost'],
        new_order['shipping_cost'],
        new_order['customs'],
        new_order['vat']
    ], 0.00)
    new_order['overall_cost'] = round(new_order['overall_cost'], 2)
    new_order['local_overall_cost'] = new_order['overall_cost'] * new_order['exchange_rate']
    new_order['markup'] = round((new_order['overall_cost'] / new_order['total_cost']) - 1, 2)

    def remove_shipping_info(item):
        item.pop('shipping_info', None)
        return item

    response = new_order
    if 'create_order' in request.json['order']:
        response = store_items_and_create_order(new_order, usd_to_kes)
    else:
        response['items'] = map(remove_shipping_info, response['items'])

    payload = json.dumps(response)
    return Response(payload, status=200, mimetype='application/json')

def sync_users_order_to_hostgator(endpoint, order_key):
    """sync this order with user info to Hostgator admin servers"""
    order = order_key.get()
    order_payload = order.to_dict()
    order_payload["id"] = order_key.id()
    order_payload["user_id"] = order.user.get().key.id()
    payload = json.dumps({
        "order" : order_payload,
    })
    logging.info("Payload: {}".frmat(payload))
    resource = "{endpoint}/order".format(endpoint=endpoint)
    response = requests.post(resource, data=payload)

    logging.info("Response Status Code: {status_code}, Response Body: {body}".format(
        status_code=response.status_code,
        body=response.text
    ))

@orders.route("/order/<string:order_id>", methods=["PUT", "PATCH"])
def relate_user_to_their_order(order_id):
    """Adds user to the order they created for relational purpose"""
    order_key = Key(urlsafe=order_id)
    order = order_key.get()

    if order is not None:
        logging.info("posted-user:{}".format(request.json["user"]))
        posted_user = json.loads(request.json["user"])
        user_key = Key(User, posted_user["id"])
        user = user_key.get()

        if user is not None:
            order.user = user_key
            order.put()

            deffered.defer(sync_users_order_to_hostgator, endpoint, order_key)

            timestamp = "{:%Y-%m-%d %H:%M:%S}".format(datetime.now())
            payload = json.dumps({"timestamp", timestamp})
            return Response(payload, status=200, mimetype="application/json")

        payload = json.dumps({"message" : "error/user-not-found"})
        return Response(payload, status=404, mimetype="application/json")

    payload = json.dumps({"message" : "error/order-not-found"})
    return Response(payload, status=404, mimetype="application/json")

@orders.route("/order/<string:order_id>/payment", methods=["GET"])
def get_order_payment_details(order_id):
    order_key = Key(urlsafe=order_id)
    order = order_key.get()

    payment = order.paypal_payment.get()
    payload = payment.to_dict()
    payload["id"] = payment.key.id()
    payload["order_id"] = order_key.id()
    payload = json.dumps(payload)
    return Response(payload, status=200, mimetype="application/json")