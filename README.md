# Jira API Support Export

CLI สำหรับค้นหา Jira issue ใต้ `API Support Bucket (SOSP-3)` ที่เป็น `Request` หรือ `Task`,
มีไฟล์แนบ `.yaml`, `.yml`, หรือ `.json`, แล้ว export เป็น CSV

## Setup

ต้องใช้ Python 3 เท่านั้น ไม่ต้องติดตั้ง dependency เพิ่ม

```bash
export JIRA_EMAIL="your.email@pttep.com"
export JIRA_API_TOKEN="your-jira-api-token"
export JIRA_BASE_URL="https://pttep.atlassian.net"
```

## Run

```bash
python3 jira_export.py
```

ผลลัพธ์ default คือ `jira_api_support_export.csv`
และ default จะ export แค่ 10 card ก่อน

```bash
python3 jira_export.py --output sosp_export.csv
```

ถ้าต้องการเปลี่ยนจำนวน card:

```bash
python3 jira_export.py --limit 50
```

ถ้าต้องการไม่จำกัดจำนวน:

```bash
python3 jira_export.py --limit 0
```

ถ้าต้องการทดสอบ card เดียว:

```bash
python3 jira_export.py --issue-key SOSP-32465 --output sosp_32465.csv
```

ถ้าชื่อ issue type ใน Jira ไม่ตรง `Request` หรือ `Task` ให้ override ได้:

```bash
python3 jira_export.py --issue-types "Request,Task,Service Request"
```

หรือใช้ JQL เองทั้งหมด:

```bash
python3 jira_export.py --jql 'parent = SOSP-3 AND issuetype in ("Request", "Task") ORDER BY created DESC'
```

## Columns

- `link`: full Jira issue link
- `title`: Jira summary
- `dev`: `true`/`false` จาก title + description
- `qa`: `true`/`false`, รวมคำว่า `sit`
- `uat`: `true`/`false`, รวมคำว่า `staging` และ `stage`
- `prod`: `true`/`false`, รวมคำว่า `production`
- `basepath`: พยายามดึงจาก title + description หลายค่าใช้ comma คั่น
- `create date`: วันที่สร้าง issue จาก Jira

## Notes

- สคริปต์อ่าน credential จาก environment variable เท่านั้น
- เงื่อนไขไฟล์แนบเช็คจาก filename extension `.yaml`, `.yml`, `.json`
- ไม่ download หรือ parse ไฟล์แนบ เพราะ basepath ใช้จาก title + description ตาม scope ปัจจุบัน

## Summarize Latest Card By Environment

หลังจากได้ `jira_api_support_export.csv` แล้ว สร้าง CSV แยกตาม env ได้ด้วย:

```bash
python3 summarize_latest_by_env.py
```

ไฟล์ output:

- `jira_latest_by_basepath_dev.csv`
- `jira_latest_by_basepath_qa.csv`
- `jira_latest_by_basepath_uat.csv`
- `jira_latest_by_basepath_prod.csv`

แต่ละไฟล์มี column:

- `base path`
- `card`

## Add Apigee Proxy Name

หลังจากมีไฟล์ `jira_latest_by_basepath_{env}.csv` แล้ว เพิ่มชื่อ proxy จาก Apigee ได้ด้วย:

```bash
python3 add_apigee_proxy_name.py
```

สคริปต์จะอ่าน DB config จาก `.env` (`APIGEE_SYNC_DB_*`), query table
`apigee.apigee_proxy_endpoints`, map `base path` กับ `proxy_name`,
สร้าง cache ที่ `apigee_basepath_proxy_map.csv`, แล้วเขียน output:

- `jira_latest_by_basepath_dev_with_proxy.csv`
- `jira_latest_by_basepath_qa_with_proxy.csv`
- `jira_latest_by_basepath_uat_with_proxy.csv`
- `jira_latest_by_basepath_prod_with_proxy.csv`

บรรทัดที่ map proxy ไม่ได้จะปล่อย column `proxy` ว่างไว้

ถ้ามี cache แล้วอยาก join CSV ใหม่โดยไม่ยิง Apigee API ซ้ำ:

```bash
python3 add_apigee_proxy_name.py --use-cache
```

ถ้าต้องการกลับไปดึงจาก Apigee API โดยตรง:

```bash
gcloud auth login
gcloud config set project gcp-pttep-th-it-apimgmt
python3 add_apigee_proxy_name.py --source api
```
