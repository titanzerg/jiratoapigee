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

ผลลัพธ์ default คือ `data/jira_export/jira_api_support_export.csv`
และ default จะ export ทุก card ที่ match เงื่อนไข

```bash
python3 jira_export.py --output data/jira_export/sosp_export.csv
```

ถ้าต้องการจำกัดจำนวน card:

```bash
python3 jira_export.py --limit 50
```

ถ้าต้องการไม่จำกัดจำนวน:

```bash
python3 jira_export.py --limit 0
```

ถ้าต้องการทดสอบ card เดียว:

```bash
python3 jira_export.py --issue-key SOSP-32465 --output data/jira_export/sosp_32465.csv
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

หลังจากได้ `data/jira_export/jira_api_support_export.csv` แล้ว สร้าง CSV แยกตาม env ได้ด้วย:

```bash
python3 summarize_latest_by_env.py
```

ไฟล์ output:

- `data/summarize_latest_by_env/jira_latest_by_basepath_dev.csv`
- `data/summarize_latest_by_env/jira_latest_by_basepath_qa.csv`
- `data/summarize_latest_by_env/jira_latest_by_basepath_uat.csv`
- `data/summarize_latest_by_env/jira_latest_by_basepath_prod.csv`

แต่ละไฟล์มี column:

- `base path`
- `card`

## Add Apigee Proxy Name

หลังจากมีไฟล์ `data/summarize_latest_by_env/jira_latest_by_basepath_{env}.csv` แล้ว เพิ่มชื่อ proxy จาก Apigee ได้ด้วย:

```bash
python3 add_apigee_proxy_name.py
```

สคริปต์จะอ่าน DB config จาก `.env` (`APIGEE_SYNC_DB_*`), query table
`apigee.apigee_proxy_endpoints`, map `base path` กับ `proxy_name`,
สร้าง cache ที่ `data/add_apigee_proxy_name/apigee_basepath_proxy_map.csv`, แล้วเขียน output:

- `data/add_apigee_proxy_name/jira_latest_by_basepath_dev_with_proxy.csv`
- `data/add_apigee_proxy_name/jira_latest_by_basepath_qa_with_proxy.csv`
- `data/add_apigee_proxy_name/jira_latest_by_basepath_uat_with_proxy.csv`
- `data/add_apigee_proxy_name/jira_latest_by_basepath_prod_with_proxy.csv`

บรรทัดที่ map proxy ไม่ได้จะปล่อย column `proxy` ว่างไว้
ยกเว้นกรณี card เดียวกันมี basepath อื่นที่ map proxy ได้แล้ว แถว basepath ที่ map ไม่ได้ของ card นั้นจะถูกซ่อนไม่เขียนลงไฟล์ `_with_proxy.csv`

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

mode นี้จะเพิ่ม column `confident` โดยเทียบ path ใน swagger attachment ของ Jira card
กับ path จาก flow condition ใน Apigee revision bundle

ตอนใช้ `--source api` สคริปต์จะ cache revision bundle zip ไว้ใน folder:

```text
data/add_apigee_proxy_name/apigee_bundle_cache/
```

และ cache swagger paths จาก Jira ไว้ใน folder:

```text
data/add_apigee_proxy_name/jira_swagger_path_cache/
```

รอบถัดไปจะอ่านจาก folder cache ก่อน ถ้าไม่มีไฟล์ค่อยโหลดใหม่

เปลี่ยน cache folder ได้:

```bash
python3 add_apigee_proxy_name.py --source api --bundle-cache-dir /path/to/bundle-cache --swagger-cache-dir /path/to/swagger-cache
```

## Guess Missing Proxy

ถ้าต้องการเดา proxy สำหรับ row ที่ `proxy` ว่างในไฟล์ `data/add_apigee_proxy_name/jira_latest_by_basepath_*_with_proxy.csv`:

```bash
python3 guess_missing_proxy.py
```

output default:

```text
data/guess_missing_proxy/jira_missing_proxy_guess.csv
```

column:

- `card`
- `guess_proxy`
- `confident`
- `desc_basepath`

`desc_basepath` เป็นข้อความสำหรับ copy ไปใส่ description เช่น `basepath: /xxx`

ถ้า confidence ต่ำกว่า 50% หรือเดาไม่ได้ จะปล่อย `guess_proxy`, `confident`, และ `desc_basepath` ว่าง

## Update Jira Basepath

เพิ่มหรือแก้บรรทัด `basepath: ...` ใน description ของ card:

```bash
python3 updatebasepath SOSP-32172 "/running_club"
```

ลองก่อนโดยยังไม่ update จริง:

```bash
python3 updatebasepath SOSP-32172 "/running_club" --dry-run
```

อัปเดตหลาย card จากไฟล์ guess โดยเลือกเฉพาะ confidence ที่ถึง threshold:

```bash
python3 updatebasepath_from_guess.py --confident 80
```

คำสั่งด้านบนเป็น dry-run default ถ้าจะ update จริง:

```bash
python3 updatebasepath_from_guess.py --confident 80 --apply
```
