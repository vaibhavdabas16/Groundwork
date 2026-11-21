# Data quality report - Olist Brazilian E-Commerce

_Generated 2026-11-19T18:01:58+00:00 by `groundwork quality`; every number below comes from SQL executed against the real warehouse (`data/manifests/dq_report.json`)._

## Source

- Dataset: Brazilian E-Commerce Public Dataset by Olist (https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)
- License: CC BY-NC-SA 4.0 (verified from https://www.kaggle.com/api/v1/datasets/view/olistbr/brazilian-ecommerce)
- Retrieved: 2026-11-19T17:37:51+00:00 via `kaggle` from https://storage.googleapis.com/kaggle-data-sets/55151/2669146/bundle/archive.zip
- Archive SHA-256: `967e41e04fc306fe604e2a693f488995a8b41e5047418f8a5c8e4abd6deca784`

| file | SHA-256 | bytes |
|---|---|---|
| olist_customers_dataset.csv | `983a422239e1712ded753b3bf9ecf47dc73f144d306029dcfa99e70a226883d2` | 9,033,957 |
| olist_geolocation_dataset.csv | `b514f6fc991b9566aeba02aa5d67e2c3630f034b60a0e05aa0d082a3b66d88d6` | 61,273,883 |
| olist_order_items_dataset.csv | `0bc4d068c4fe38cbb01bd90e8746e3c613fe7b4baef75fab7b0e329701c3e279` | 15,438,671 |
| olist_order_payments_dataset.csv | `4f713964f2815dbbaa40b9488268c55aac3627bfce5aa96cf58d1f3616de3cc0` | 5,777,138 |
| olist_order_reviews_dataset.csv | `012b61c7593e34f51fa614efdf802b9c7056ce6aae5307ddb93236e7cfc797d7` | 14,451,670 |
| olist_orders_dataset.csv | `8df58ef3d2d7e9944010f7beecd9b75367f5588ec6e3c91cec19ae3345ef9ecf` | 17,654,914 |
| olist_products_dataset.csv | `3e6569628a17fbc75fd206ee357b59e20364b9afa90f5b6cd5b4d624c58aa9cc` | 2,379,446 |
| olist_sellers_dataset.csv | `1f643d2b950373b85735e7794b20986f528d7a000432e7c6f9bcbb44d0846a0e` | 174,703 |
| product_category_name_translation.csv | `a81f0d1f27b27e7293f761bc79e3ce8f348ee39c4b3ed3e49bde38f478586278` | 2,613 |

## Tables, row counts and grain

| table | rows | grain |
|---|---|---|
| raw.customers | 99,441 | one row per customer_id - which is issued PER ORDER (not per person) |
| raw.geolocation | 1,000,163 | one row per source point; NOT unique by ZIP prefix |
| raw.order_items | 112,650 | one row per (order_id, order_item_id) |
| raw.order_payments | 103,886 | one row per (order_id, payment_sequential) |
| raw.order_reviews | 99,224 | one row per (review_id, order_id); neither column is unique alone |
| raw.orders | 99,441 | one row per order_id |
| raw.products | 32,951 | one row per product_id |
| raw.sellers | 3,095 | one row per seller_id |
| raw.product_category_name_translation | 71 | one row per product_category_name |

Serving (agent-facing) relations:

| relation | rows |
|---|---|
| analytics.orders | 99,441 |
| analytics.customers | 99,441 |
| analytics.order_items | 112,650 |
| analytics.order_payments | 103,886 |
| analytics.order_reviews | 99,224 |
| analytics.products | 32,951 |
| analytics.sellers | 3,095 |
| analytics.product_category_name_translation | 71 |
| analytics.geolocation_zip_prefix | 19,015 |
| analytics.order_item_totals | 98,666 |
| analytics.order_payment_totals | 99,440 |
| analytics.order_review_summary | 98,673 |

## Invariants (must hold)

21/21 passed.

| check | description | violations | result |
|---|---|---|---|
| `pk_unique.orders` | (order_id) is unique and non-null in raw.orders | 0 | PASS |
| `pk_unique.customers` | (customer_id) is unique and non-null in raw.customers | 0 | PASS |
| `pk_unique.order_items` | (order_id, order_item_id) is unique and non-null in raw.order_items | 0 | PASS |
| `pk_unique.order_payments` | (order_id, payment_sequential) is unique and non-null in raw.order_payments | 0 | PASS |
| `pk_unique.order_reviews` | (review_id, order_id) is unique and non-null in raw.order_reviews | 0 | PASS |
| `pk_unique.products` | (product_id) is unique and non-null in raw.products | 0 | PASS |
| `pk_unique.sellers` | (seller_id) is unique and non-null in raw.sellers | 0 | PASS |
| `pk_unique.product_category_name_translation` | (product_category_name) is unique and non-null in raw.product_category_name_translation | 0 | PASS |
| `fk.orders.customer_id->customers` | every raw.orders.customer_id exists in raw.customers.customer_id | 0 | PASS |
| `fk.order_items.order_id->orders` | every raw.order_items.order_id exists in raw.orders.order_id | 0 | PASS |
| `fk.order_items.product_id->products` | every raw.order_items.product_id exists in raw.products.product_id | 0 | PASS |
| `fk.order_items.seller_id->sellers` | every raw.order_items.seller_id exists in raw.sellers.seller_id | 0 | PASS |
| `fk.order_payments.order_id->orders` | every raw.order_payments.order_id exists in raw.orders.order_id | 0 | PASS |
| `fk.order_reviews.order_id->orders` | every raw.order_reviews.order_id exists in raw.orders.order_id | 0 | PASS |
| `grain.customer_id_one_order` | customer_id is per-order: every customers.customer_id appears in exactly one order | 0 | PASS |
| `range.review_score_1_5` | review_score is between 1 and 5 | 0 | PASS |
| `range.price_positive` | order_items.price > 0 | 0 | PASS |
| `range.freight_non_negative` | order_items.freight_value >= 0 | 0 | PASS |
| `range.payment_value_non_negative` | order_payments.payment_value >= 0 | 0 | PASS |
| `time.purchase_not_null` | every order has a purchase timestamp | 0 | PASS |
| `time.delivered_after_purchase` | customer delivery is never before purchase | 0 | PASS |

## Observed properties and violations of the real data

These are documented, not 'fixed'. The baseline in `data/manifests/dq_baseline.json` detects upstream changes.

| check | category | description | observed |
|---|---|---|---|
| `dup.review_id_across_orders` | keys | review_ids that appear on more than one order | 789 |
| `grain.orders_with_multiple_reviews` | grain | orders with more than one review row | 547 |
| `grain.orders_with_multiple_payments` | grain | orders with more than one payment row | 2961 |
| `grain.max_payment_rows_per_order` | grain | maximum payment rows for a single order | 29 |
| `grain.orders_with_multiple_items` | grain | orders with more than one item row | 9803 |
| `grain.max_items_per_order` | grain | maximum item rows for a single order | 21 |
| `grain.orders_with_multiple_sellers` | grain | orders whose items come from more than one seller | 1278 |
| `grain.customer_unique_id_multiple_customer_ids` | grain | customer_unique_ids (people) mapped to more than one customer_id (order-level id) | 2997 |
| `grain.distinct_customer_unique_id` | grain | distinct customer_unique_id values | 96096 |
| `grain.geolocation_rows_per_zip_prefix_max` | grain | max geolocation rows sharing one ZIP prefix | 1146 |
| `grain.geolocation_distinct_zip_prefix` | grain | distinct ZIP prefixes in geolocation | 19015 |
| `grain.geolocation_zip_prefix_multiple_states` | grain | ZIP prefixes mapped to more than one state | 8 |
| `ri.orders_without_items` | referential_integrity | orders with no order_items rows | 775 |
| `ri.orders_without_payments` | referential_integrity | orders with no payment rows | 1 |
| `ri.orders_without_reviews` | referential_integrity | orders with no review rows | 768 |
| `ri.customer_zip_not_in_geolocation` | referential_integrity | customers whose ZIP prefix is absent from geolocation | 278 |
| `ri.seller_zip_not_in_geolocation` | referential_integrity | sellers whose ZIP prefix is absent from geolocation | 7 |
| `ri.product_category_missing_translation` | referential_integrity | distinct non-null categories without English translation | 2 |
| `null.product_category` | missing_values | products with NULL category | 610 |
| `consistency.payment_vs_items_mismatch_orders` | consistency | orders (with both items and payments) where sum(payment_value) differs from sum(price+freight) by more than 0.01 | 303 |
| `consistency.zero_value_payments` | consistency | payment rows with payment_value = 0 | 9 |
| `consistency.payment_type_not_defined` | consistency | payment rows with payment_type = 'not_defined' | 3 |
| `consistency.zero_installments` | consistency | payment rows with payment_installments = 0 | 2 |
| `status.delivered_without_delivery_date` | timestamps | status 'delivered' but no customer delivery date | 8 |
| `status.not_delivered_with_delivery_date` | timestamps | status other than 'delivered' but has a delivery date | 6 |
| `time.carrier_before_purchase` | timestamps | carrier handoff timestamp earlier than purchase timestamp | 166 |
| `time.carrier_before_approval` | timestamps | carrier handoff timestamp earlier than approval timestamp | 1359 |
| `time.delivered_before_carrier` | timestamps | customer delivery earlier than carrier handoff | 23 |
| `time.max_purchase_date_epoch` | timestamps | latest purchase timestamp (epoch seconds) - the dataset 'as-of' date | 1539797418 (2018-10-17 17:30:18) |
| `outlier.items_price_above_p99_9` | outliers | item rows with price above the 99.9th percentile | 105 |
| `outlier.freight_exceeds_price` | outliers | item rows where freight_value exceeds price | 4124 |
| `fanout.naive_payment_item_review_join_inflation_cents` | fanout | inflation (in cents) of sum(payment_value) when payments are naively joined to items and reviews | 440797042 cents = BRL 4,407,970.42 |

## Null percentages and ranges

### raw.customers

| column | type | null % | ~distinct | min | max |
|---|---|---|---|---|---|
| customer_id | VARCHAR | 0.0 | 97,769 |  |  |
| customer_unique_id | VARCHAR | 0.0 | 98,358 |  |  |
| customer_zip_code_prefix | VARCHAR | 0.0 | 15,086 |  |  |
| customer_city | VARCHAR | 0.0 | 4,186 |  |  |
| customer_state | VARCHAR | 0.0 | 26 |  |  |

### raw.geolocation

| column | type | null % | ~distinct | min | max |
|---|---|---|---|---|---|
| geolocation_zip_code_prefix | VARCHAR | 0.0 | 19,365 |  |  |
| geolocation_lat | DOUBLE | 0.0 | 727,986 | -36.6053744107061 | 45.06593318269697 |
| geolocation_lng | DOUBLE | 0.0 | 706,899 | -101.46676644931476 | 121.10539381057764 |
| geolocation_city | VARCHAR | 0.0 | 7,782 |  |  |
| geolocation_state | VARCHAR | 0.0 | 26 |  |  |

### raw.order_items

| column | type | null % | ~distinct | min | max |
|---|---|---|---|---|---|
| order_id | VARCHAR | 0.0 | 98,921 |  |  |
| order_item_id | INTEGER | 0.0 | 21 | 1 | 21 |
| product_id | VARCHAR | 0.0 | 32,523 |  |  |
| seller_id | VARCHAR | 0.0 | 3,080 |  |  |
| shipping_limit_date | TIMESTAMP | 0.0 | 89,981 | 2016-09-19 00:15:34 | 2020-04-09 22:35:08 |
| price | DECIMAL(12,2) | 0.0 | 6,029 | 0.85 | 6735.00 |
| freight_value | DECIMAL(12,2) | 0.0 | 6,772 | 0.00 | 409.68 |

### raw.order_payments

| column | type | null % | ~distinct | min | max |
|---|---|---|---|---|---|
| order_id | VARCHAR | 0.0 | 99,974 |  |  |
| payment_sequential | INTEGER | 0.0 | 29 | 1 | 29 |
| payment_type | VARCHAR | 0.0 | 5 |  |  |
| payment_installments | INTEGER | 0.0 | 24 | 0 | 24 |
| payment_value | DECIMAL(12,2) | 0.0 | 28,934 | 0.00 | 13664.08 |

### raw.order_reviews

| column | type | null % | ~distinct | min | max |
|---|---|---|---|---|---|
| review_id | VARCHAR | 0.0 | 97,803 |  |  |
| order_id | VARCHAR | 0.0 | 99,168 |  |  |
| review_score | INTEGER | 0.0 | 5 | 1 | 5 |
| review_comment_title | VARCHAR | 88.342 | 4,538 |  |  |
| review_comment_message | VARCHAR | 58.703 | 36,834 |  |  |
| review_creation_date | TIMESTAMP | 0.0 | 623 | 2016-10-02 00:00:00 | 2018-08-31 00:00:00 |
| review_answer_timestamp | TIMESTAMP | 0.0 | 101,131 | 2016-10-07 18:32:28 | 2018-10-29 12:27:35 |

### raw.orders

| column | type | null % | ~distinct | min | max |
|---|---|---|---|---|---|
| order_id | VARCHAR | 0.0 | 99,974 |  |  |
| customer_id | VARCHAR | 0.0 | 97,769 |  |  |
| order_status | VARCHAR | 0.0 | 8 |  |  |
| order_purchase_timestamp | TIMESTAMP | 0.0 | 96,571 | 2016-09-04 21:15:19 | 2018-10-17 17:30:18 |
| order_approved_at | TIMESTAMP | 0.161 | 87,279 | 2016-09-15 12:16:38 | 2018-09-03 17:40:06 |
| order_delivered_carrier_date | TIMESTAMP | 1.793 | 81,859 | 2016-10-08 10:34:01 | 2018-09-11 19:48:28 |
| order_delivered_customer_date | TIMESTAMP | 2.982 | 93,526 | 2016-10-11 13:46:32 | 2018-10-17 13:22:46 |
| order_estimated_delivery_date | TIMESTAMP | 0.0 | 451 | 2016-09-30 00:00:00 | 2018-11-12 00:00:00 |

### raw.products

| column | type | null % | ~distinct | min | max |
|---|---|---|---|---|---|
| product_id | VARCHAR | 0.0 | 32,523 |  |  |
| product_category_name | VARCHAR | 1.851 | 71 |  |  |
| product_name_lenght | INTEGER | 1.851 | 66 | 5 | 76 |
| product_description_lenght | INTEGER | 1.851 | 2,902 | 4 | 3992 |
| product_photos_qty | INTEGER | 1.851 | 19 | 1 | 20 |
| product_weight_g | INTEGER | 0.006 | 2,234 | 0 | 40425 |
| product_length_cm | INTEGER | 0.006 | 100 | 7 | 105 |
| product_height_cm | INTEGER | 0.006 | 103 | 2 | 105 |
| product_width_cm | INTEGER | 0.006 | 95 | 6 | 118 |

### raw.sellers

| column | type | null % | ~distinct | min | max |
|---|---|---|---|---|---|
| seller_id | VARCHAR | 0.0 | 3,080 |  |  |
| seller_zip_code_prefix | VARCHAR | 0.0 | 2,276 |  |  |
| seller_city | VARCHAR | 0.0 | 604 |  |  |
| seller_state | VARCHAR | 0.0 | 22 |  |  |

### raw.product_category_name_translation

| column | type | null % | ~distinct | min | max |
|---|---|---|---|---|---|
| product_category_name | VARCHAR | 0.0 | 69 |  |  |
| product_category_name_english | VARCHAR | 0.0 | 73 |  |  |

## Join and metric pitfalls (derived from the checks above)

- **Order vs item grain**: monetary values live on `order_items` (price, freight) and `order_payments`; orders has none. Joining orders to items yields item grain.
- **Payments x items x reviews**: joining all three on order_id inflates sum(payment_value) - see `fanout.naive_payment_item_review_join_inflation_cents`. Use `analytics.order_*_totals` / `order_review_summary`.
- **customer_id is per order**: count people with customer_unique_id (`customer_unique_key` in serving).
- **review_id is not unique** and some orders have multiple reviews.
- **Geolocation is not keyed by ZIP prefix**: joining customers/sellers to raw geolocation fans out; use `analytics.geolocation_zip_prefix`.
- **Status vs timestamps disagree** for a handful of orders; carrier timestamps sometimes precede purchase/approval.
- **Historical data**: last purchase 2018-10-17. Relative windows must be anchored to the data, not today.
- **Payments != items + freight** for some orders; payments also exist for orders without items.
