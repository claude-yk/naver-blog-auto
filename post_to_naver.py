"""
네이버 블로그 자동 발행 스크립트

동작 방식:
1. posts/ 폴더에서 아직 발행하지 않은 글(.md)을 파일명 순서대로 하나 고른다.
2. 브라우저를 열어 네이버에 로그인한다. (아이디/비번은 .env 에서 읽음. 코드에 하드코딩하지 않음)
3. 블로그 글쓰기 페이지로 이동해 제목/본문을 채운다.
4. 기본값은 아무 버튼도 누르지 않고 멈춘다(dry-run, 미리보기만).
   --draft 옵션을 주면 "임시저장" 버튼을 눌러 비공개 임시저장으로 남긴다.
   --publish 옵션을 주면 실제로 "발행" 버튼까지 눌러 공개 발행한다.
5. --draft 또는 --publish로 성공하면 해당 글 파일을 posts/published/ 로 옮겨서
   다음 실행 때 중복 처리되지 않게 한다.

주의:
- 네이버가 자동화 로그인을 의심해 보안문자(캡차)나 새로운 환경 인증을 띄우는 경우가 있다.
  이 스크립트는 그런 인증을 우회하려는 어떠한 시도도 하지 않는다. 해당 화면이 뜨면
  스크립트는 멈춰서 사람이 직접 브라우저에서 인증을 완료하길 기다린다.
- 네이버 블로그 에디터(SmartEditor)의 화면 구조는 네이버 쪽에서 수시로 바뀔 수 있다.
  아래 CONFIG의 셀렉터가 더 이상 맞지 않으면 F12 개발자 도구로 직접 확인해서 값을 바꿔야 한다.
"""

import argparse
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service

BASE_DIR = Path(__file__).resolve().parent
POSTS_DIR = BASE_DIR / "posts"
PUBLISHED_DIR = POSTS_DIR / "published"

# 네이버 쪽 화면이 바뀌면 이 부분만 고쳐서 쓰면 된다.
CONFIG = {
    "login_url": "https://nid.naver.com/nidlogin.login",
    "login_id_selector": (By.ID, "id"),
    "login_pw_selector": (By.ID, "pw"),
    "blog_write_url": "https://blog.naver.com/{blog_id}?Redirect=Write",
    "editor_iframe_selector": (By.ID, "mainFrame"),
    "title_area_selector": (By.CSS_SELECTOR, ".se-documentTitle .se-text-paragraph"),
    "body_area_selector": (By.CSS_SELECTOR, ".se-component.se-text .se-text-paragraph"),
    "publish_open_selector": (By.CSS_SELECTOR, "button.publish_btn__m9KHH"),
    "publish_confirm_selector": (By.CSS_SELECTOR, "button.confirm_btn__WEaBq"),
    # 텍스트 기반 셀렉터라 CSS 클래스(해시값)가 바뀌어도 비교적 안정적이지만,
    # 그래도 실제 화면에서 버튼 문구가 다르면 F12로 확인해서 바꿔야 한다.
    "save_draft_selector": (By.XPATH, "//button[contains(., '저장') and not(contains(., '자동'))]"),
}


def parse_post(md_path: Path):
    text = md_path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not match:
        raise ValueError(f"{md_path.name}: front matter(---) 형식이 올바르지 않습니다.")

    front_matter, body = match.groups()
    meta = {}
    for line in front_matter.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()

    return {
        "title": meta.get("title", md_path.stem),
        "tags": [t.strip() for t in meta.get("tags", "").split(",") if t.strip()],
        "body": body.strip(),
    }


def next_post_file():
    PUBLISHED_DIR.mkdir(parents=True, exist_ok=True)
    candidates = sorted(p for p in POSTS_DIR.glob("*.md") if p.is_file())
    return candidates[0] if candidates else None


def wait_for_manual_verification(driver):
    """
    네이버가 로그인 시 캡차/추가 인증 화면을 띄우는지 확인한다.
    감지되면 자동으로 아무것도 하지 않고, 사람이 직접 처리할 때까지 기다린다.
    """
    suspicious_markers = ["captcha", "deviceConfirm", "nsecure"]
    current_url = driver.current_url
    if any(marker.lower() in current_url.lower() for marker in suspicious_markers):
        print("\n[안내] 네이버가 추가 보안 인증을 요구하고 있습니다.")
        print("브라우저 창에서 직접 인증을 완료한 뒤, 이 터미널에서 Enter 키를 눌러주세요.")
        input("계속하려면 Enter... ")


def login(driver, naver_id, naver_pw):
    driver.get(CONFIG["login_url"])
    wait = WebDriverWait(driver, 15)

    id_field = wait.until(EC.presence_of_element_located(CONFIG["login_id_selector"]))
    pw_field = driver.find_element(*CONFIG["login_pw_selector"])

    id_field.click()
    id_field.send_keys(naver_id)
    pw_field.click()
    pw_field.send_keys(naver_pw)

    # 로그인 버튼의 id/class는 네이버 쪽에서 자주 바뀌므로,
    # 버튼을 직접 찾아 클릭하는 대신 비밀번호 입력창에서 Enter로 폼을 제출한다.
    pw_field.send_keys(Keys.RETURN)
    time.sleep(2)

    wait_for_manual_verification(driver)

    # 로그인 완료(네이버 홈 또는 블로그로 리다이렉트) 확인
    try:
        WebDriverWait(driver, 20).until(
            lambda d: "nidlogin" not in d.current_url
        )
    except TimeoutException:
        print("[오류] 로그인 완료 여부를 확인하지 못했습니다. 브라우저 화면을 직접 확인해주세요.")
        print(f"[진단] 현재 URL: {driver.current_url}")
        print(f"[진단] 페이지 제목: {driver.title}")
        input("문제를 해결한 뒤 Enter... ")


def write_post(driver, blog_id, post):
    url = CONFIG["blog_write_url"].format(blog_id=blog_id)
    driver.get(url)
    wait = WebDriverWait(driver, 20)

    try:
        wait.until(EC.frame_to_be_available_and_switch_to_it(CONFIG["editor_iframe_selector"]))
    except TimeoutException:
        print("[오류] 블로그 글쓰기 화면(iframe)을 찾지 못했습니다.")
        print(f"[진단] 현재 URL: {driver.current_url}")
        print(f"[진단] 페이지 제목: {driver.title}")
        raise

    # "이어서 작성하시겠습니까" 같은 이전 글 팝업이 뜨면 취소하고 새 글로 시작
    try:
        cancel_btn = driver.find_element(By.CSS_SELECTOR, "button.se-popup-button-cancel")
        cancel_btn.click()
        time.sleep(1)
    except NoSuchElementException:
        pass

    title_area = wait.until(EC.element_to_be_clickable(CONFIG["title_area_selector"]))
    title_area.click()
    title_area.send_keys(post["title"])

    body_area = driver.find_element(*CONFIG["body_area_selector"])
    body_area.click()
    for line in post["body"].splitlines():
        body_area.send_keys(line)
        body_area.send_keys(Keys.ENTER)

    print(f"\n[미리보기] 제목: {post['title']}")
    print(f"[미리보기] 태그: {', '.join(post['tags'])}")
    print("브라우저에서 제목/본문이 올바르게 들어갔는지 직접 확인해주세요.")


def publish_post(driver):
    wait = WebDriverWait(driver, 15)
    open_btn = wait.until(EC.element_to_be_clickable(CONFIG["publish_open_selector"]))
    open_btn.click()
    time.sleep(1)

    confirm_btn = wait.until(EC.element_to_be_clickable(CONFIG["publish_confirm_selector"]))
    confirm_btn.click()
    time.sleep(2)


def save_draft(driver):
    """공개 발행 대신 비공개 임시저장 버튼을 누른다."""
    wait = WebDriverWait(driver, 15)
    save_btn = wait.until(EC.element_to_be_clickable(CONFIG["save_draft_selector"]))
    save_btn.click()
    time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description="네이버 블로그 자동 발행")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--draft",
        action="store_true",
        help="비공개 임시저장 버튼까지 클릭한다. (공개 발행 아님)",
    )
    group.add_argument(
        "--publish",
        action="store_true",
        help="실제로 발행 버튼까지 클릭해서 공개 발행한다.",
    )
    args = parser.parse_args()

    load_dotenv(BASE_DIR / ".env")
    naver_id = os.getenv("NAVER_ID")
    naver_pw = os.getenv("NAVER_PW")
    blog_id = os.getenv("NAVER_BLOG_ID")

    if not all([naver_id, naver_pw, blog_id]):
        print("[오류] .env 파일에 NAVER_ID, NAVER_PW, NAVER_BLOG_ID 를 모두 채워주세요.")
        print(".env.example 을 복사해서 .env 로 만든 뒤 값을 입력하면 됩니다.")
        sys.exit(1)

    post_file = next_post_file()
    if post_file is None:
        print("발행할 글이 없습니다. posts/ 폴더에 .md 파일을 추가해주세요.")
        sys.exit(0)

    post = parse_post(post_file)
    print(f"이번에 발행할 글: {post_file.name}")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service)

    try:
        login(driver, naver_id, naver_pw)
        write_post(driver, blog_id, post)

        if args.publish:
            input("\n확인했다면 Enter를 눌러 실제로 발행합니다 (Ctrl+C로 취소 가능)... ")
            publish_post(driver)
            PUBLISHED_DIR.mkdir(parents=True, exist_ok=True)
            post_file.rename(PUBLISHED_DIR / post_file.name)
            print(f"발행 완료: {post_file.name}")
        elif args.draft:
            input("\n확인했다면 Enter를 눌러 임시저장합니다 (Ctrl+C로 취소 가능)... ")
            save_draft(driver)
            PUBLISHED_DIR.mkdir(parents=True, exist_ok=True)
            post_file.rename(PUBLISHED_DIR / post_file.name)
            print(f"임시저장 완료: {post_file.name} (공개 발행 아님, 블로그 관리자 페이지에서 확인)")
        else:
            print("\n옵션이 없어 저장/발행은 하지 않았습니다. (dry-run, 미리보기만)")
            input("확인 후 Enter를 누르면 브라우저를 닫습니다... ")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()
