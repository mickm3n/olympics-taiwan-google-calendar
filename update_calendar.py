import requests
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import datetime
import time
import os
from dateutil.parser import parse
from dateutil import tz
from opencc import OpenCC
from requests.exceptions import RequestException

# 設置 Google Calendar API
SCOPES = ["https://www.googleapis.com/auth/calendar"]
CALENDAR_ID = os.environ["CALENDAR_ID"]

# 初始化 OpenCC 轉換器
cc = OpenCC("s2twp")  # 簡體轉繁體（台灣正體）


def convert_to_traditional(text):
    return cc.convert(text)


def get_olympic_schedule(max_retries=3, retry_delay=5):
    url = "https://sph-s-api.olympics.com/summer/schedules/api/CHI/schedule/noc/TPE"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": "https://olympics.com",
        "Referer": "https://olympics.com/",
    }

    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()  # 如果響應碼不是 200，將引發 HTTPError 異常
            return response.json()
        except RequestException as e:
            print(f"嘗試 {attempt + 1}/{max_retries} 失敗：{str(e)}")
            if attempt + 1 < max_retries:
                print(f"等待 {retry_delay} 秒後重試...")
                time.sleep(retry_delay)
            else:
                print("達到最大重試次數，無法獲取數據。")
                return None

    return None  # 如果所有嘗試都失敗，返回 None


def get_olympic_schedule_from_file():
    with open("schedule.json", "r") as f:
        return json.load(f)


class CalendarUpdater:
    GAME_CALENDAR_MAPPING_FILE_PATH = "data/game_calendar_mapping.json"
    TAIWAN_ATHLETES_NAME_MAPPING_FILE_PATH = "data/taiwan_athlete_name_mapping.json"

    def __init__(self, calendar_id) -> None:
        self.calendar_id = calendar_id
        with open(self.GAME_CALENDAR_MAPPING_FILE_PATH, "r") as f:
            self.game_calendar_mapping = json.load(f)
        with open(self.TAIWAN_ATHLETES_NAME_MAPPING_FILE_PATH, "r") as f:
            self.taiwan_athletes_name_mapping = json.load(f)
        self._init_calendar_service()

    def _init_calendar_service(self):
        if "GOOGLE_SERVICE_ACCOUNT_CREDENTIALS_JSON" in os.environ:
            service_account_info = json.loads(
                os.getenv("GOOGLE_SERVICE_ACCOUNT_CREDENTIALS_JSON")
            )
            creds = service_account.Credentials.from_service_account_info(
                service_account_info, scopes=SCOPES
            )
        else:
            creds = service_account.Credentials.from_service_account_file(
                "service_account.json", scopes=SCOPES
            )
        self.calendar_service = build("calendar", "v3", credentials=creds)

    def store_mapping(self):
        with open(self.GAME_CALENDAR_MAPPING_FILE_PATH, "w") as f:
            json.dump(self.game_calendar_mapping, f)

    def update_calendar(self):
        schedule = get_olympic_schedule()

        if not schedule:
            print("Failed to get schedule")
            return

        for unit in schedule.get("units", []):
            start_time = parse(unit["startDate"])
            end_time = parse(unit["endDate"])

            # Skip if the end_time is more than three hour ago before now
            if end_time + datetime.timedelta(hours=3) < datetime.datetime.now(
                tz=tz.gettz("Europe/Paris")
            ):
                continue

            # 如果有台灣選手參賽，添加到描述中
            taiwan_athletes = [
                self.taiwan_athletes_name_mapping.get(comp["name"], comp["name"])
                for comp in unit.get("competitors", [])
                if comp.get("noc", "") == "TPE"
            ]

            description = (
                convert_to_traditional(
                    f"比賽詳細內容：https://olympics.com{unit.get('extraData', {}).get('detailUrl', '')}"
                )
                if unit.get("extraData", {}).get("detailUrl", "")
                else ""
            )

            event = {
                "summary": convert_to_traditional(
                    f"{', '.join(taiwan_athletes)} - {unit['disciplineName']} - {unit['eventUnitName']}"
                ),
                "description": description,
                "start": {
                    "dateTime": start_time.isoformat(),
                    "timeZone": "Europe/Paris",
                },
                "end": {
                    "dateTime": end_time.isoformat(),
                    "timeZone": "Europe/Paris",
                },
            }

            calendar_event = self.create_or_update_event(event, unit["id"])
            if calendar_event:
                self.game_calendar_mapping[unit["id"]] = calendar_event["id"]

        self.store_mapping()

    def create_or_update_event(self, event, event_id):
        try:
            # 檢查是否已存在相同的事件
            try:
                calendar_event_id = self.game_calendar_mapping.get(event_id, "")
                calendar_event = (
                    self.calendar_service.events()
                    .get(
                        calendarId=self.calendar_id,
                        eventId=calendar_event_id,
                    )
                    .execute()
                )
            except HttpError as error:
                print(f"An error occurred: {error}")
                raise

            if not calendar_event.get("id"):
                calendar_event = (
                    self.calendar_service.events()
                    .insert(calendarId=self.calendar_id, body=event)
                    .execute()
                )
                print(
                    f'Event created: {calendar_event.get("summary")}: {calendar_event.get("htmlLink")}'
                )
            else:
                if (
                    calendar_event.get("summary") == event.get("summary")
                    and (
                        not calendar_event.get("description")
                        or calendar_event.get("description") == event.get("description")
                    )
                    and datetime.datetime.fromisoformat(
                        calendar_event.get("start").get("dateTime")
                    )
                    == datetime.datetime.fromisoformat(
                        event.get("start").get("dateTime")
                    )
                    and datetime.datetime.fromisoformat(
                        calendar_event.get("end").get("dateTime")
                    )
                    == datetime.datetime.fromisoformat(event.get("end").get("dateTime"))
                ):
                    return calendar_event
                calendar_event = (
                    self.calendar_service.events()
                    .update(
                        calendarId=self.calendar_id,
                        eventId=calendar_event["id"],
                        body=event,
                    )
                    .execute()
                )
                print(
                    f'Event updated: {calendar_event.get("summary")}: {calendar_event.get("htmlLink")}'
                )
            return calendar_event
        except HttpError as error:
            print(f"An error occurred: {error}")
            return None

    def print(self, event):
        print(
            event.get("summary"),
            event.get("description"),
            event.get("start").get("dateTime"),
            event.get("end").get("dateTime"),
        )


def main():
    calendar_updater = CalendarUpdater(CALENDAR_ID)
    calendar_updater.update_calendar()


if __name__ == "__main__":
    main()
