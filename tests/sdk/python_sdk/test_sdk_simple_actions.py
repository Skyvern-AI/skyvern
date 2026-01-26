import pytest


@pytest.mark.asyncio
async def test_clicks(web_server, skyvern_browser):
    page = await skyvern_browser.get_working_page()

    await page.goto(f"{web_server}/click.html")

    assert await page.locator("#counter").text_content() == "Button clicked 0 times"
    await page.click("#button")
    assert await page.locator("#counter").text_content() == "Button clicked 1 times"

    await page.click(prompt="Click on the button")
    assert await page.locator("#counter").text_content() == "Button clicked 2 times"

    print("Fallback")
    await page.click("#counterBroken", prompt="Click on the button")
    assert await page.locator("#counter").text_content() == "Button clicked 3 times"

    await page.click("#button")
    assert await page.locator("#counter").text_content() == "Button clicked 4 times"
    print("All tests passed")


@pytest.mark.asyncio
async def test_fill(web_server, skyvern_browser):
    page = await skyvern_browser.get_working_page()

    await page.goto(f"{web_server}/input.html")

    assert await page.locator("#output").text_content() == ""
    await page.fill("#nameInput", "Test1")
    await page.click("#submitBtn")
    assert await page.locator("#output").text_content() == "Hello, Test1!"

    await page.fill(prompt="Type 'Test2' in the name input")
    await page.click("#submitBtn")
    assert await page.locator("#output").text_content() == "Hello, Test2!"

    await page.fill(prompt="Type the value in the name input", value="Test3")
    await page.click("#submitBtn")
    assert await page.locator("#output").text_content() == "Hello, Test3!"

    # fallback
    await page.fill("#nameInputBroken", "TestFallback", prompt="Fill the name input")
    await page.click("#submitBtn")
    assert await page.locator("#output").text_content() == "Hello, TestFallback!"


@pytest.mark.asyncio
async def test_select_option(web_server, skyvern_browser):
    """Test using page.act() with natural language prompts on a combobox."""
    page = await skyvern_browser.get_working_page()

    await page.goto(f"{web_server}/combobox.html")

    await page.select_option("#cars", "audi")
    assert await page.locator("#cars").input_value() == "audi"

    await page.select_option("#cars", value="opel")
    assert await page.locator("#cars").input_value() == "opel"

    await page.select_option("#cars", label="Saab")
    assert await page.locator("#cars").input_value() == "saab"

    await page.select_option(prompt="Select 'Audi' i the car combobox")
    assert await page.locator("#cars").input_value() == "audi"

    # fallback
    await page.select_option("#cars-broken", "opel", prompt="Select 'Opel' i the car combobox")
    assert await page.locator("#cars").input_value() == "opel"

    await page.select_option("#cars-broken", label="Saab", prompt="Select 'Saab' i the car combobox")
    assert await page.locator("#cars").input_value() == "saab"


@pytest.mark.asyncio
async def test_act_combobox(web_server, skyvern_browser):
    """Test using page.act() with natural language prompts on a combobox."""
    page = await skyvern_browser.get_working_page()

    await page.goto(f"{web_server}/combobox.html")

    await page.act("select 'Audi' from the combobox")

    assert await page.locator("#cars").input_value() == "audi"


@pytest.mark.asyncio
async def test_act_input_and_click(web_server, skyvern_browser):
    page = await skyvern_browser.get_working_page()

    await page.goto(f"{web_server}/input.html")

    await page.act("type 'ActTest' into the input box")
    await page.act("click on the button")

    assert await page.locator("#output").text_content() == "Hello, ActTest!"


@pytest.mark.asyncio
async def test_upload(web_server, skyvern_browser):
    page = await skyvern_browser.get_working_page()
    image_url = "https://img.freepik.com/free-photo/portrait-beautiful-purebred-pussycat-with-shorthair-orange-collar-neck-sitting-floor-reacting-camera-flash-scared-looking-light-indoor_8353-12551.jpg?semt=ais_hybrid&w=740&q=80"

    await page.goto(f"{web_server}/upload.html")

    await page.reload()
    assert (await page.locator("#uploadedImage").get_attribute("src")).__str__() == ""
    await page.upload_file("#imageUpload", image_url)
    assert (await page.locator("#uploadedImage").get_attribute("src")).__str__().startswith("data:image/jpeg")

    await page.reload()
    assert (await page.locator("#uploadedImage").get_attribute("src")).__str__() == ""
    await page.upload_file(prompt="Upload this", files=image_url)
    assert (await page.locator("#uploadedImage").get_attribute("src")).__str__().startswith("data:image/jpeg")

    await page.reload()
    assert (await page.locator("#uploadedImage").get_attribute("src")).__str__() == ""
    await page.upload_file(prompt=f"Upload this file {image_url}")
    assert (await page.locator("#uploadedImage").get_attribute("src")).__str__().startswith("data:image/jpeg")

    print("fallback")
    await page.reload()
    assert (await page.locator("#uploadedImage").get_attribute("src")).__str__() == ""
    await page.upload_file("#imageUploadBroken", image_url, prompt="Upload this file")
    assert (await page.locator("#uploadedImage").get_attribute("src")).__str__().startswith("data:image/jpeg")
    print("all done")


@pytest.mark.asyncio
async def test_extract(web_server, skyvern_browser):
    page = await skyvern_browser.get_working_page()

    await page.goto(f"{web_server}/click.html")

    result = await page.extract("give one sentence description of this page")
    print(result)
    assert "click" in str(result)

    result = await page.extract(
        prompt="Describe this page",
        schema={
            "type": "object",
            "properties": {
                "short": {"type": "string", "description": "one sentence description of this page"},
                "long": {"type": "string", "description": "two-three sentence description of this page"},
            },
            "required": ["short", "long"],
        },
    )
    print(result)
    assert "click" in str(result)
    assert "short" in str(result)
    assert "long" in str(result)


@pytest.mark.asyncio
async def test_prompt_locator(web_server, skyvern_browser):
    page = await skyvern_browser.get_working_page()

    await page.goto(f"{web_server}/click.html")

    assert await page.locator("#counter").text_content() == "Button clicked 0 times"
    await page.locator(prompt="Find the 'click me' button").click()
    assert await page.locator("#counter").text_content() == "Button clicked 1 times"

    await page.locator(prompt="Find the 'click me' button").nth(0).click()
    assert await page.locator("#counter").text_content() == "Button clicked 2 times"

    await page.locator("#bad-selector", prompt="Find the 'click me' button").click()
    assert await page.locator("#counter").text_content() == "Button clicked 3 times"

    await page.locator("#bad-selector", prompt="Find the 'click me' button").nth(0).click()
    assert await page.locator("#counter").text_content() == "Button clicked 4 times"

    await page.locator("#button").click()
    assert await page.locator("#counter").text_content() == "Button clicked 5 times"


@pytest.mark.asyncio
async def test_prompt_locator_chaining(web_server, skyvern_browser):
    page = await skyvern_browser.get_working_page()

    await page.goto(f"{web_server}/combobox.html")

    await page.locator("#cars").select_option("opel")
    assert await page.locator("#cars").input_value() == "opel"

    await page.locator(prompt="Find the 'cars' combobox").select_option("saab")
    assert await page.locator("#cars").input_value() == "saab"

    await page.locator("#bad-selector", prompt="Find the 'cars' combobox").select_option("audi")
    assert await page.locator("#cars").input_value() == "audi"


@pytest.mark.asyncio
async def test_validate(web_server, skyvern_browser):
    page = await skyvern_browser.get_working_page()

    await page.goto(f"{web_server}/click.html")
    await page.click("#button")

    is_valid = await page.validate("if clicked time > 0")
    assert is_valid

    is_valid = await page.validate("if clicked time > 1")
    assert not is_valid

    # invalid prompt does not pass validation
    is_valid = await page.validate("the input text is valid")
    assert not is_valid


@pytest.mark.asyncio
async def test_prompt(skyvern_browser):
    page = await skyvern_browser.get_working_page()

    r = await page.prompt("1111+1111")
    print(r)
    assert "2222" in str(r)

    r = await page.prompt(
        "2+2",
        schema={
            "type": "object",
            "properties": {
                "result_number": {"type": "int"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
    )
    print(r)
    assert r["result_number"] == 4
