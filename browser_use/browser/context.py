"""
Playwright browser on steroids.
"""

import asyncio
import base64
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, TypedDict

from playwright.async_api import Browser as PlaywrightBrowser
from playwright.async_api import (
	BrowserContext as PlaywrightBrowserContext,
)
from playwright.async_api import (
	ElementHandle,
	FrameLocator,
	Page,
)

from browser_use.browser.views import BrowserError, BrowserState, TabInfo, URLNotAllowedError
from browser_use.dom.service import DomService
from browser_use.dom.views import DOMElementNode, SelectorMap
from browser_use.utils import time_execution_sync

if TYPE_CHECKING:
	from browser_use.browser.browser import Browser

logger = logging.getLogger(__name__)


class BrowserContextWindowSize(TypedDict):
	width: int
	height: int


@dataclass
class BrowserContextConfig:
	"""
	Configuration for the BrowserContext.

	Default values:
		cookies_file: None
			Path to cookies file for persistence

	        disable_security: False
	                Disable browser security features

		minimum_wait_page_load_time: 0.5
			Minimum time to wait before getting page state for LLM input

	        wait_for_network_idle_page_load_time: 1.0
	                Time to wait for network requests to finish before getting page state.
	                Lower values may result in incomplete page loads.

		maximum_wait_page_load_time: 5.0
			Maximum time to wait for page load before proceeding anyway

		wait_between_actions: 1.0
			Time to wait between multiple per step actions

		browser_window_size: {
				'width': 1280,
				'height': 1100,
			}
			Default browser window size

		no_viewport: False
			Disable viewport

		save_recording_path: None
			Path to save video recordings

		trace_path: None
			Path to save trace files. It will auto name the file with the TRACE_PATH/{context_id}.zip

		locale: None
			Specify user locale, for example en-GB, de-DE, etc. Locale will affect navigator.language value, Accept-Language request header value as well as number and date formatting rules. If not provided, defaults to the system default locale.

		user_agent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/85.0.4183.102 Safari/537.36'
			custom user agent to use.

		highlight_elements: True
			Highlight elements in the DOM on the screen

		viewport_expansion: 500
			Viewport expansion in pixels. This amount will increase the number of elements which are included in the state what the LLM will see. If set to -1, all elements will be included (this leads to high token usage). If set to 0, only the elements which are visible in the viewport will be included.

		allowed_domains: None
			List of allowed domains that can be accessed. If None, all domains are allowed.
			Example: ['example.com', 'api.example.com']
	"""

	cookies_file: str | None = None
	minimum_wait_page_load_time: float = 0.5
	wait_for_network_idle_page_load_time: float = 1
	maximum_wait_page_load_time: float = 5
	wait_between_actions: float = 1

	disable_security: bool = False

	browser_window_size: BrowserContextWindowSize = field(default_factory=lambda: {'width': 1280, 'height': 1100})
	no_viewport: Optional[bool] = None

	save_recording_path: str | None = None
	trace_path: str | None = None
	locale: str | None = None
	user_agent: str = (
		'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36  (KHTML, like Gecko) Chrome/85.0.4183.102 Safari/537.36'
	)

	highlight_elements: bool = True
	viewport_expansion: int = 500
	allowed_domains: list[str] | None = None


@dataclass
class BrowserSession:
	context: PlaywrightBrowserContext
	current_page: Page
	cached_state: BrowserState


class BrowserContext:
	def __init__(
		self,
		browser: 'Browser',
		config: BrowserContextConfig = BrowserContextConfig(),
	):
		self.context_id = str(uuid.uuid4())
		logger.debug(f'Initializing new browser context with id: {self.context_id}')

		self.config = config
		self.browser = browser

		# Initialize these as None - they'll be set up when needed
		self.session: BrowserSession | None = None

	async def __aenter__(self):
		"""Async context manager entry"""
		await self._initialize_session()
		return self

	async def __aexit__(self, exc_type, exc_val, exc_tb):
		"""Async context manager exit"""
		await self.close()

	async def close(self):
		"""Close the browser instance"""
		logger.debug('Closing browser context')

		try:
			# check if already closed
			if self.session is None:
				return

			await self.save_cookies()

			if self.config.trace_path:
				try:
					await self.session.context.tracing.stop(path=os.path.join(self.config.trace_path, f'{self.context_id}.zip'))
				except Exception as e:
					logger.debug(f'Failed to stop tracing: {e}')

			try:
				await self.session.context.close()
			except Exception as e:
				logger.debug(f'Failed to close context: {e}')
		finally:
			self.session = None

	def __del__(self):
		"""Cleanup when object is destroyed"""
		if self.session is not None:
			logger.debug('BrowserContext was not properly closed before destruction')
			try:
				# Use sync Playwright method for force cleanup
				if hasattr(self.session.context, '_impl_obj'):
					asyncio.run(self.session.context._impl_obj.close())
				self.session = None
			except Exception as e:
				logger.warning(f'Failed to force close browser context: {e}')

	async def _initialize_session(self):
		"""Initialize the browser session"""
		logger.debug('Initializing browser context')

		playwright_browser = await self.browser.get_playwright_browser()

		context = await self._create_context(playwright_browser)
		self._add_new_page_listener(context)
		page = await context.new_page()

		# Instead of calling _update_state(), create an empty initial state
		initial_state = self._get_initial_state(page)

		self.session = BrowserSession(
			context=context,
			current_page=page,
			cached_state=initial_state,
		)
		return self.session

	def _add_new_page_listener(self, context: PlaywrightBrowserContext):
		async def on_page(page: Page):
			await page.wait_for_load_state()
			logger.debug(f'New page opened: {page.url}')
			if self.session is not None:
				self.session.current_page = page

		context.on('page', on_page)

	async def get_session(self) -> BrowserSession:
		"""Lazy initialization of the browser and related components"""
		if self.session is None:
			return await self._initialize_session()
		return self.session

	async def get_current_page(self) -> Page:
		"""Get the current page"""
		session = await self.get_session()
		return session.current_page

	async def _create_context(self, browser: PlaywrightBrowser):
		"""Creates a new browser context with anti-detection measures and loads cookies if available."""
		if self.browser.config.cdp_url and len(browser.contexts) > 0:
			context = browser.contexts[0]
		elif self.browser.config.chrome_instance_path and len(browser.contexts) > 0:
			# Connect to existing Chrome instance instead of creating new one
			context = browser.contexts[0]
		else:
			# Original code for creating new context
			context = await browser.new_context(
				viewport=self.config.browser_window_size,
				no_viewport=False,
				user_agent=self.config.user_agent,
				java_script_enabled=True,
				bypass_csp=self.config.disable_security,
				ignore_https_errors=self.config.disable_security,
				record_video_dir=self.config.save_recording_path,
				record_video_size=self.config.browser_window_size,
				locale=self.config.locale,
			)

		if self.config.trace_path:
			await context.tracing.start(screenshots=True, snapshots=True, sources=True)

		# Load cookies if they exist
		if self.config.cookies_file and os.path.exists(self.config.cookies_file):
			with open(self.config.cookies_file, 'r') as f:
				cookies = json.load(f)
				logger.info(f'Loaded {len(cookies)} cookies from {self.config.cookies_file}')
				await context.add_cookies(cookies)

		# Expose anti-detection scripts
		await context.add_init_script(
			"""
			// Webdriver property
			Object.defineProperty(navigator, 'webdriver', {
				get: () => undefined
			});

			// Languages
			Object.defineProperty(navigator, 'languages', {
				get: () => ['en-US']
			});

			// Plugins
			Object.defineProperty(navigator, 'plugins', {
				get: () => [1, 2, 3, 4, 5]
			});

			// Chrome runtime
			window.chrome = { runtime: {} };

			// Permissions
			const originalQuery = window.navigator.permissions.query;
			window.navigator.permissions.query = (parameters) => (
				parameters.name === 'notifications' ?
					Promise.resolve({ state: Notification.permission }) :
					originalQuery(parameters)
			);
			(function () {
				const originalAttachShadow = Element.prototype.attachShadow;
				Element.prototype.attachShadow = function attachShadow(options) {
					return originalAttachShadow.call(this, { ...options, mode: "open" });
				};
			})();
			"""
		)

		return context

	async def _wait_for_stable_network(self):
		page = await self.get_current_page()

		pending_requests = set()
		last_activity = asyncio.get_event_loop().time()

		# Define relevant resource types and content types
		RELEVANT_RESOURCE_TYPES = {
			'document',
			'stylesheet',
			'image',
			'font',
			'script',
			'iframe',
		}

		RELEVANT_CONTENT_TYPES = {
			'text/html',
			'text/css',
			'application/javascript',
			'image/',
			'font/',
			'application/json',
		}

		# Additional patterns to filter out
		IGNORED_URL_PATTERNS = {
			# Analytics and tracking
			'analytics',
			'tracking',
			'telemetry',
			'beacon',
			'metrics',
			# Ad-related
			'doubleclick',
			'adsystem',
			'adserver',
			'advertising',
			# Social media widgets
			'facebook.com/plugins',
			'platform.twitter',
			'linkedin.com/embed',
			# Live chat and support
			'livechat',
			'zendesk',
			'intercom',
			'crisp.chat',
			'hotjar',
			# Push notifications
			'push-notifications',
			'onesignal',
			'pushwoosh',
			# Background sync/heartbeat
			'heartbeat',
			'ping',
			'alive',
			# WebRTC and streaming
			'webrtc',
			'rtmp://',
			'wss://',
			# Common CDNs for dynamic content
			'cloudfront.net',
			'fastly.net',
		}

		async def on_request(request):
			# Filter by resource type
			if request.resource_type not in RELEVANT_RESOURCE_TYPES:
				return

			# Filter out streaming, websocket, and other real-time requests
			if request.resource_type in {
				'websocket',
				'media',
				'eventsource',
				'manifest',
				'other',
			}:
				return

			# Filter out by URL patterns
			url = request.url.lower()
			if any(pattern in url for pattern in IGNORED_URL_PATTERNS):
				return

			# Filter out data URLs and blob URLs
			if url.startswith(('data:', 'blob:')):
				return

			# Filter out requests with certain headers
			headers = request.headers
			if headers.get('purpose') == 'prefetch' or headers.get('sec-fetch-dest') in [
				'video',
				'audio',
			]:
				return

			nonlocal last_activity
			pending_requests.add(request)
			last_activity = asyncio.get_event_loop().time()
			# logger.debug(f'Request started: {request.url} ({request.resource_type})')

		async def on_response(response):
			request = response.request
			if request not in pending_requests:
				return

			# Filter by content type if available
			content_type = response.headers.get('content-type', '').lower()

			# Skip if content type indicates streaming or real-time data
			if any(
				t in content_type
				for t in [
					'streaming',
					'video',
					'audio',
					'webm',
					'mp4',
					'event-stream',
					'websocket',
					'protobuf',
				]
			):
				pending_requests.remove(request)
				return

			# Only process relevant content types
			if not any(ct in content_type for ct in RELEVANT_CONTENT_TYPES):
				pending_requests.remove(request)
				return

			# Skip if response is too large (likely not essential for page load)
			content_length = response.headers.get('content-length')
			if content_length and int(content_length) > 5 * 1024 * 1024:  # 5MB
				pending_requests.remove(request)
				return

			nonlocal last_activity
			pending_requests.remove(request)
			last_activity = asyncio.get_event_loop().time()
			# logger.debug(f'Request resolved: {request.url} ({content_type})')

		# Attach event listeners
		page.on('request', on_request)
		page.on('response', on_response)

		try:
			# Wait for idle time
			start_time = asyncio.get_event_loop().time()
			while True:
				await asyncio.sleep(0.1)
				now = asyncio.get_event_loop().time()
				if len(pending_requests) == 0 and (now - last_activity) >= self.config.wait_for_network_idle_page_load_time:
					break
				if now - start_time > self.config.maximum_wait_page_load_time:
					logger.debug(
						f'Network timeout after {self.config.maximum_wait_page_load_time}s with {len(pending_requests)} '
						f'pending requests: {[r.url for r in pending_requests]}'
					)
					break

		finally:
			# Clean up event listeners
			page.remove_listener('request', on_request)
			page.remove_listener('response', on_response)

		logger.debug(f'Network stabilized for {self.config.wait_for_network_idle_page_load_time} seconds')

	async def _wait_for_page_and_frames_load(self, timeout_overwrite: float | None = None):
		"""
		Ensures page is fully loaded before continuing.
		Waits for either network to be idle or minimum WAIT_TIME, whichever is longer.
		Also checks if the loaded URL is allowed.
		"""
		# Start timing
		start_time = time.time()

		# Wait for page load
		try:
			await self._wait_for_stable_network()

			# Check if the loaded URL is allowed
			page = await self.get_current_page()
			await self._check_and_handle_navigation(page)
		except URLNotAllowedError as e:
			raise e
		except Exception:
			logger.warning('Page load failed, continuing...')
			pass

		# Calculate remaining time to meet minimum WAIT_TIME
		elapsed = time.time() - start_time
		remaining = max((timeout_overwrite or self.config.minimum_wait_page_load_time) - elapsed, 0)

		logger.debug(f'--Page loaded in {elapsed:.2f} seconds, waiting for additional {remaining:.2f} seconds')

		# Sleep remaining time if needed
		if remaining > 0:
			await asyncio.sleep(remaining)

	def _is_url_allowed(self, url: str) -> bool:
		"""Check if a URL is allowed based on the whitelist configuration."""
		if not self.config.allowed_domains:
			return True

		try:
			from urllib.parse import urlparse

			parsed_url = urlparse(url)
			domain = parsed_url.netloc.lower()

			# Remove port number if present
			if ':' in domain:
				domain = domain.split(':')[0]

			# Check if domain matches any allowed domain pattern
			return any(
				domain == allowed_domain.lower() or domain.endswith('.' + allowed_domain.lower())
				for allowed_domain in self.config.allowed_domains
			)
		except Exception as e:
			logger.error(f'Error checking URL allowlist: {str(e)}')
			return False

	async def _check_and_handle_navigation(self, page: Page) -> None:
		"""Check if current page URL is allowed and handle if not."""
		if not self._is_url_allowed(page.url):
			logger.warning(f'Navigation to non-allowed URL detected: {page.url}')
			try:
				await self.go_back()
			except Exception as e:
				logger.error(f'Failed to go back after detecting non-allowed URL: {str(e)}')
			raise URLNotAllowedError(f'Navigation to non-allowed URL: {page.url}')

	async def navigate_to(self, url: str):
		"""Navigate to a URL"""
		if not self._is_url_allowed(url):
			raise BrowserError(f'Navigation to non-allowed URL: {url}')

		page = await self.get_current_page()
		await page.goto(url)
		await page.wait_for_load_state()

	async def refresh_page(self):
		"""Refresh the current page"""
		page = await self.get_current_page()
		await page.reload()
		await page.wait_for_load_state()

	async def go_back(self):
		"""Navigate back in history"""
		page = await self.get_current_page()
		try:
			# 10 ms timeout
			await page.go_back(timeout=10, wait_until='domcontentloaded')
			# await self._wait_for_page_and_frames_load(timeout_overwrite=1.0)
		except Exception as e:
			# Continue even if its not fully loaded, because we wait later for the page to load
			logger.debug(f'During go_back: {e}')

	async def go_forward(self):
		"""Navigate forward in history"""
		page = await self.get_current_page()
		try:
			await page.go_forward(timeout=10, wait_until='domcontentloaded')
		except Exception as e:
			# Continue even if its not fully loaded, because we wait later for the page to load
			logger.debug(f'During go_forward: {e}')

	async def close_current_tab(self):
		"""Close the current tab"""
		session = await self.get_session()
		page = session.current_page
		await page.close()

		# Switch to the first available tab if any exist
		if session.context.pages:
			await self.switch_to_tab(0)

		# otherwise the browser will be closed

	async def get_page_html(self) -> str:
		"""Get the current page HTML content"""
		page = await self.get_current_page()
		return await page.content()

	async def execute_javascript(self, script: str):
		"""Execute JavaScript code on the page"""
		page = await self.get_current_page()
		return await page.evaluate(script)

	@time_execution_sync('--get_state')  # This decorator might need to be updated to handle async
	async def get_state(self, use_vision: bool = False) -> BrowserState:
		"""Get the current state of the browser"""
		await self._wait_for_page_and_frames_load()
		session = await self.get_session()
		session.cached_state = await self._update_state(use_vision=use_vision)

		# Save cookies if a file is specified
		if self.config.cookies_file:
			asyncio.create_task(self.save_cookies())

		return session.cached_state

	async def _update_state(self, use_vision: bool = False, focus_element: int = -1) -> BrowserState:
		"""Update and return state."""
		session = await self.get_session()

		# Check if current page is still valid, if not switch to another available page
		try:
			page = await self.get_current_page()
			# Test if page is still accessible
			await page.evaluate('1')
		except Exception as e:
			logger.debug(f'Current page is no longer accessible: {str(e)}')
			# Get all available pages
			pages = session.context.pages
			if pages:
				session.current_page = pages[-1]
				page = session.current_page
				logger.debug(f'Switched to page: {await page.title()}')
			else:
				raise BrowserError('Browser closed: no valid pages available')

		try:
			await self.remove_highlights()
			dom_service = DomService(page)
			content = await dom_service.get_clickable_elements(
				focus_element=focus_element,
				viewport_expansion=self.config.viewport_expansion,
				highlight_elements=self.config.highlight_elements,
			)

			screenshot_b64 = None
			if use_vision:
				screenshot_b64 = await self.take_screenshot()
			pixels_above, pixels_below = await self.get_scroll_info(page)

			self.current_state = BrowserState(
				element_tree=content.element_tree,
				selector_map=content.selector_map,
				url=page.url,
				title=await page.title(),
				tabs=await self.get_tabs_info(),
				screenshot=screenshot_b64,
				pixels_above=pixels_above,
				pixels_below=pixels_below,
			)

			return self.current_state
		except Exception as e:
			logger.error(f'Failed to update state: {str(e)}')
			# Return last known good state if available
			if hasattr(self, 'current_state'):
				return self.current_state
			raise

	# region - Browser Actions

	async def take_screenshot(self, full_page: bool = False) -> str:
		"""
		Returns a base64 encoded screenshot of the current page.
		"""
		page = await self.get_current_page()

		screenshot = await page.screenshot(
			full_page=full_page,
			animations='disabled',
		)

		screenshot_b64 = base64.b64encode(screenshot).decode('utf-8')

		# await self.remove_highlights()

		return screenshot_b64

	async def remove_highlights(self):
		"""
		Removes all highlight overlays and labels created by the highlightElement function.
		Handles cases where the page might be closed or inaccessible.
		"""
		try:
			page = await self.get_current_page()
			await page.evaluate(
				"""
                try {
                    // Remove the highlight container and all its contents
                    const container = document.getElementById('playwright-highlight-container');
                    if (container) {
                        container.remove();
                    }

                    // Remove highlight attributes from elements
                    const highlightedElements = document.querySelectorAll('[browser-user-highlight-id^="playwright-highlight-"]');
                    highlightedElements.forEach(el => {
                        el.removeAttribute('browser-user-highlight-id');
                    });
                } catch (e) {
                    console.error('Failed to remove highlights:', e);
                }
                """
			)
		except Exception as e:
			logger.debug(f'Failed to remove highlights (this is usually ok): {str(e)}')
			# Don't raise the error since this is not critical functionality
			pass

	# endregion

	# region - User Actions
	def _convert_simple_xpath_to_css_selector(self, xpath: str) -> str:
		"""Converts simple XPath expressions to CSS selectors."""
		if not xpath:
			return ''

		# Remove leading slash if present
		xpath = xpath.lstrip('/')

		# Split into parts
		parts = xpath.split('/')
		css_parts = []

		for part in parts:
			if not part:
				continue

			# Handle index notation [n]
			if '[' in part:
				base_part = part[: part.find('[')]
				index_part = part[part.find('[') :]

				# Handle multiple indices
				indices = [i.strip('[]') for i in index_part.split(']')[:-1]]

				for idx in indices:
					try:
						# Handle numeric indices
						if idx.isdigit():
							index = int(idx) - 1
							base_part += f':nth-of-type({index + 1})'
						# Handle last() function
						elif idx == 'last()':
							base_part += ':last-of-type'
						# Handle position() functions
						elif 'position()' in idx:
							if '>1' in idx:
								base_part += ':nth-of-type(n+2)'
					except ValueError:
						continue

				css_parts.append(base_part)
			else:
				css_parts.append(part)

		base_selector = ' > '.join(css_parts)
		return base_selector

	def _enhanced_css_selector_for_element(self, element: DOMElementNode) -> str:
		"""
		Creates a CSS selector for a DOM element, handling various edge cases and special characters.

		Args:
		        element: The DOM element to create a selector for

		Returns:
		        A valid CSS selector string
		"""
		try:
			# Get base selector from XPath
			css_selector = self._convert_simple_xpath_to_css_selector(element.xpath)

			# Handle class attributes
			if 'class' in element.attributes and element.attributes['class']:
				# Define a regex pattern for valid class names in CSS
				valid_class_name_pattern = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_-]*$')

				# Iterate through the class attribute values
				classes = element.attributes['class'].split()
				for class_name in classes:
					# Skip empty class names
					if not class_name.strip():
						continue

					# Check if the class name is valid
					if valid_class_name_pattern.match(class_name):
						# Append the valid class name to the CSS selector
						css_selector += f'.{class_name}'
					else:
						# Skip invalid class names
						continue

			# Expanded set of safe attributes that are stable and useful for selection
			SAFE_ATTRIBUTES = {
				# Standard HTML attributes
				'id',
				'name',
				'type',
				'value',
				'placeholder',
				# Accessibility attributes
				'aria-label',
				'aria-labelledby',
				'aria-describedby',
				'role',
				# Common form attributes
				'for',
				'autocomplete',
				'required',
				'readonly',
				# Media attributes
				'alt',
				'title',
				'src',
				# Data attributes (if they're stable in your application)
				'data-testid',
				'data-id',
				'data-qa',
				'data-cy',
				# Custom stable attributes (add any application-specific ones)
				'href',
				'target',
			}

			# Handle other attributes
			for attribute, value in element.attributes.items():
				if attribute == 'class':
					continue

				# Skip invalid attribute names
				if not attribute.strip():
					continue

				if attribute not in SAFE_ATTRIBUTES:
					continue

				# Escape special characters in attribute names
				safe_attribute = attribute.replace(':', r'\:')

				# Handle different value cases
				if value == '':
					css_selector += f'[{safe_attribute}]'
				elif any(char in value for char in '"\'<>`\n\r\t'):
					# Use contains for values with special characters
					# Regex-substitute *any* whitespace with a single space, then strip.
					collapsed_value = re.sub(r'\s+', ' ', value).strip()
					# Escape embedded double-quotes.
					safe_value = collapsed_value.replace('"', '\\"')
					css_selector += f'[{safe_attribute}*="{safe_value}"]'
				else:
					css_selector += f'[{safe_attribute}="{value}"]'

			return css_selector

		except Exception:
			# Fallback to a more basic selector if something goes wrong
			tag_name = element.tag_name or '*'
			return f"{tag_name}[highlight_index='{element.highlight_index}']"

	async def get_locate_element(self, element: DOMElementNode) -> ElementHandle | None:
		current_frame = await self.get_current_page()

		# Start with the target element and collect all parents
		parents: list[DOMElementNode] = []
		current = element
		while current.parent is not None:
			parent = current.parent
			parents.append(parent)
			current = parent

		# Reverse the parents list to process from top to bottom
		parents.reverse()

		# Process all iframe parents in sequence
		iframes = [item for item in parents if item.tag_name == 'iframe']
		for parent in iframes:
			css_selector = self._enhanced_css_selector_for_element(parent)
			current_frame = current_frame.frame_locator(css_selector)

		css_selector = self._enhanced_css_selector_for_element(element)

		try:
			if isinstance(current_frame, FrameLocator):
				return await current_frame.locator(css_selector).element_handle()
			else:
				# Try to scroll into view if hidden
				element_handle = await current_frame.query_selector(css_selector)
				if element_handle:
					await element_handle.scroll_into_view_if_needed()
					return element_handle
		except Exception as e:
			logger.error(f'Failed to locate element: {str(e)}')
			return None

	async def _input_text_element_node(self, element_node: DOMElementNode, text: str):
		try:
			# Highlight before typing
			if element_node.highlight_index is not None:
				await self._update_state(focus_element=element_node.highlight_index)

			page = await self.get_current_page()
			element = await self.get_locate_element(element_node)

			if element is None:
				raise Exception(f'Element: {repr(element_node)} not found')

			await element.scroll_into_view_if_needed(timeout=2500)
			await element.fill('')
			await element.type(text)
			await page.wait_for_load_state()

		except Exception as e:
			raise Exception(f'Failed to input text into element: {repr(element_node)}. Error: {str(e)}')

	async def _click_element_node(self, element_node: DOMElementNode):
		"""
		Optimized method to click an element using xpath.
		"""
		page = await self.get_current_page()

		try:
			# Highlight before clicking
			if element_node.highlight_index is not None:
				await self._update_state(focus_element=element_node.highlight_index)

			element = await self.get_locate_element(element_node)

			if element is None:
				raise Exception(f'Element: {repr(element_node)} not found')

			# await element.scroll_into_view_if_needed()

			try:
				await element.click(timeout=1500)
				await page.wait_for_load_state()
				# Check if navigation occurred and if the new URL is allowed
				await self._check_and_handle_navigation(page)
			except URLNotAllowedError as e:
				raise e
			except Exception:
				try:
					await page.evaluate('(el) => el.click()', element)
					await page.wait_for_load_state()
					# Check if navigation occurred and if the new URL is allowed
					await self._check_and_handle_navigation(page)
				except URLNotAllowedError as e:
					raise e
				except Exception as e:
					raise Exception(f'Failed to click element: {str(e)}')

		except URLNotAllowedError as e:
			raise e
		except Exception as e:
			raise Exception(f'Failed to click element: {repr(element_node)}. Error: {str(e)}')

	async def get_tabs_info(self) -> list[TabInfo]:
		"""Get information about all tabs"""
		session = await self.get_session()

		tabs_info = []
		for page_id, page in enumerate(session.context.pages):
			tab_info = TabInfo(page_id=page_id, url=page.url, title=await page.title())
			tabs_info.append(tab_info)

		return tabs_info

	async def switch_to_tab(self, page_id: int) -> None:
		"""Switch to a specific tab by its page_id

		@You can also use negative indices to switch to tabs from the end (Pure pythonic way)
		"""
		session = await self.get_session()
		pages = session.context.pages

		if page_id >= len(pages):
			raise BrowserError(f'No tab found with page_id: {page_id}')

		page = pages[page_id]

		# Check if the tab's URL is allowed before switching
		if not self._is_url_allowed(page.url):
			raise BrowserError(f'Cannot switch to tab with non-allowed URL: {page.url}')

		session.current_page = page

		await page.bring_to_front()
		await page.wait_for_load_state()

	async def create_new_tab(self, url: str | None = None) -> None:
		"""Create a new tab and optionally navigate to a URL"""
		if url and not self._is_url_allowed(url):
			raise BrowserError(f'Cannot create new tab with non-allowed URL: {url}')

		session = await self.get_session()
		new_page = await session.context.new_page()
		session.current_page = new_page

		await new_page.wait_for_load_state()

		page = await self.get_current_page()

		if url:
			await page.goto(url)
			await self._wait_for_page_and_frames_load(timeout_overwrite=1)

	# endregion

	# region - Helper methods for easier access to the DOM
	async def get_selector_map(self) -> SelectorMap:
		session = await self.get_session()
		return session.cached_state.selector_map

	async def get_element_by_index(self, index: int) -> ElementHandle | None:
		selector_map = await self.get_selector_map()
		return await self.get_locate_element(selector_map[index])

	async def get_dom_element_by_index(self, index: int) -> DOMElementNode | None:
		selector_map = await self.get_selector_map()
		return selector_map[index]

	async def save_cookies(self):
		"""Save current cookies to file"""
		if self.session and self.session.context and self.config.cookies_file:
			try:
				cookies = await self.session.context.cookies()
				logger.info(f'Saving {len(cookies)} cookies to {self.config.cookies_file}')

				# Check if the path is a directory and create it if necessary
				dirname = os.path.dirname(self.config.cookies_file)
				if dirname:
					os.makedirs(dirname, exist_ok=True)

				with open(self.config.cookies_file, 'w') as f:
					json.dump(cookies, f)
			except Exception as e:
				logger.warning(f'Failed to save cookies: {str(e)}')

	async def is_file_uploader(self, element_node: DOMElementNode, max_depth: int = 3, current_depth: int = 0) -> bool:
		"""Check if element or its children are file uploaders"""
		if current_depth > max_depth:
			return False

		# Check current element
		is_uploader = False

		if not isinstance(element_node, DOMElementNode):
			return False

		# Check for file input attributes
		if element_node.tag_name == 'input':
			is_uploader = element_node.attributes.get('type') == 'file' or element_node.attributes.get('accept') is not None

		if is_uploader:
			return True

		# Recursively check children
		if element_node.children and current_depth < max_depth:
			for child in element_node.children:
				if isinstance(child, DOMElementNode):
					if await self.is_file_uploader(child, max_depth, current_depth + 1):
						return True

		return False

	async def get_scroll_info(self, page: Page) -> tuple[int, int]:
		"""Get scroll position information for the current page."""
		scroll_y = await page.evaluate('window.scrollY')
		viewport_height = await page.evaluate('window.innerHeight')
		total_height = await page.evaluate('document.documentElement.scrollHeight')
		pixels_above = scroll_y
		pixels_below = total_height - (scroll_y + viewport_height)
		return pixels_above, pixels_below

	async def reset_context(self):
		"""Reset the browser session
		Call this when you don't want to kill the context but just kill the state
		"""
		# close all tabs and clear cached state
		session = await self.get_session()

		pages = session.context.pages
		for page in pages:
			await page.close()

		session.cached_state = self._get_initial_state()
		session.current_page = await session.context.new_page()

	def _get_initial_state(self, page: Optional[Page] = None) -> BrowserState:
		"""Get the initial state of the browser"""
		return BrowserState(
			element_tree=DOMElementNode(
				tag_name='root',
				is_visible=True,
				parent=None,
				xpath='',
				attributes={},
				children=[],
			),
			selector_map={},
			url=page.url if page else '',
			title='',
			screenshot=None,
			tabs=[],
		)
