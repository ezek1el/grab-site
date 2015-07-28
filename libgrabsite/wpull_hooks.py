import re
import os
import sys
import json
import pprint
import signal
import trollius as asyncio
from urllib.request import urlopen
from autobahn.asyncio.websocket import WebSocketClientFactory, WebSocketClientProtocol
from libgrabsite.ignoracle import Ignoracle, parameterize_record_info

real_stdout_write = sys.stdout.buffer.write
real_stderr_write = sys.stderr.buffer.write

def print_to_real(s):
	real_stdout_write((s + "\n").encode("utf-8"))
	sys.stdout.buffer.flush()


class GrabberClientProtocol(WebSocketClientProtocol):
	def on_open(self):
		self.factory.client = self
		self.send_object({
			"type": "hello",
			"mode": "grabber",
			"url": job_data["url"]
		})

	def on_close(self, was_clean, code, reason):
		self.factory.client = None
		print_to_real(
			"Disconnected from ws:// server with (was_clean, code, reason): {!r}"
				.format((was_clean, code, reason)))
		asyncio.ensure_future(connect_to_server())

	def send_object(self, obj):
		self.sendMessage(json.dumps(obj).encode("utf-8"))

	onOpen = on_open
	onClose = on_close


class GrabberClientFactory(WebSocketClientFactory):
	protocol = GrabberClientProtocol

	def __init__(self):
		super().__init__()
		self.client = None


ws_factory = GrabberClientFactory()

class Decayer(object):
	def __init__(self, initial, multiplier, maximum):
		"""
		initial - initial number to return
		multiplier - multiply number by this value after each call to decay()
		maximum - cap number at this value
		"""
		self.initial = initial
		self.multiplier = multiplier
		self.maximum = maximum
		self.reset()

	def reset(self):
		# First call to .decay() will multiply, but we want to get the `intitial`
		# value on the first call to .decay(), so divide.
		self.current = self.initial / self.multiplier
		return self.current

	def decay(self):
		self.current = min(self.current * self.multiplier, self.maximum)
		return self.current


@asyncio.coroutine
def connect_to_server():
	host = os.environ.get('GRAB_SITE_WS_HOST', '127.0.0.1')
	port = int(os.environ.get('GRAB_SITE_WS_PORT', 29001))
	decayer = Decayer(0.25, 1.5, 8)
	while True:
		try:
			coro = yield from loop.create_connection(ws_factory, host, port)
		except OSError:
			delay = decayer.decay()
			print_to_real(
				"Could not connect to ws://{}:{}, retrying in {:.1f} seconds..."
					.format(host, port, delay))
			yield from asyncio.sleep(delay)
		else:
			print_to_real("Connected to ws://{}:{}".format(host, port))
			break

loop = asyncio.get_event_loop()
asyncio.ensure_future(connect_to_server())

def graceful_stop_callback():
	print_to_real("\n^C detected, creating 'stop' file, please wait for exit...")
	with open(os.path.join(working_dir, "stop"), "wb") as f:
		pass

def forceful_stop_callback():
	loop.stop()

loop.add_signal_handler(signal.SIGINT, graceful_stop_callback)
loop.add_signal_handler(signal.SIGTERM, forceful_stop_callback)


igset_cache = {}
def get_patterns_for_ignore_set(name):
	assert name != "", name
	if name in igset_cache:
		return igset_cache[name]
	print_to_real("Fetching ArchiveBot/master/db/ignore_patterns/%s.json" % name)
	igset_cache[name] = json.loads(urlopen(
		"https://raw.githubusercontent.com/ArchiveTeam/ArchiveBot/" +
		"master/db/ignore_patterns/%s.json" % name).read().decode("utf-8")
	)["patterns"]
	return igset_cache[name]

working_dir = os.environ['GRAB_SITE_WORKING_DIR']

def mtime(f):
	return os.stat(f).st_mtime


class FileChangedWatcher(object):
	def __init__(self, fname):
		self.fname = fname
		self.last_mtime = mtime(fname)

	def has_changed(self):
		now_mtime = mtime(self.fname)
		changed = mtime(self.fname) != self.last_mtime
		self.last_mtime = now_mtime
		return changed


igsets_watcher = FileChangedWatcher(os.path.join(working_dir, "igsets"))
ignores_watcher = FileChangedWatcher(os.path.join(working_dir, "ignores"))

ignoracle = Ignoracle()

def update_ignoracle():
	with open(os.path.join(working_dir, "igsets"), "r") as f:
		igsets = f.read().strip("\r\n\t ,").split(',')

	with open(os.path.join(working_dir, "ignores"), "r") as f:
		ignores = set(ig for ig in f.read().strip("\r\n").split('\n') if ig != "")

	for igset in igsets:
		patterns = get_patterns_for_ignore_set(igset)
		if igset == "global":
			patterns = filter(lambda p: "archive\\.org" not in p, patterns)
		ignores.update(patterns)

	print_to_real("Using these %d ignores:" % len(ignores))
	print_to_real(pprint.pformat(ignores))

	ignoracle.set_patterns(ignores)

update_ignoracle()


def should_ignore_url(url, record_info):
	"""
	Returns whether a URL should be ignored.
	"""
	parameters = parameterize_record_info(record_info)
	return ignoracle.ignores(url, **parameters)


def accept_url(url_info, record_info, verdict, reasons):
	if igsets_watcher.has_changed() or ignores_watcher.has_changed():
		update_ignoracle()

	url = url_info['url']

	if url.startswith('data:'):
		# data: URLs aren't something you can grab, so drop them to avoid ignore
		# checking and ignore logging.
		return False

	pattern = should_ignore_url(url, record_info)
	if pattern:
		maybe_log_ignore(url, pattern)
		return False

	# If we get here, none of our ignores apply.	Return the original verdict.
	return verdict


def queued_url(url_info):
	job_data["items_queued"] += 1


def dequeued_url(url_info, record_info):
	job_data["items_downloaded"] += 1


job_data = {
	"ident": open(os.path.join(working_dir, "id")).read().strip(),
	"url": open(os.path.join(working_dir, "start_url")).read().strip(),
	"started_at": os.stat(os.path.join(working_dir, "start_url")).st_mtime,
	"suppress_ignore_reports": True,
	"concurrency": int(open(os.path.join(working_dir, "concurrency")).read().strip()),
	"bytes_downloaded": 0,
	"items_queued": 0,
	"items_downloaded": 0,
	"delay_min": 0,
	"delay_max": 0,
	"r1xx": 0,
	"r2xx": 0,
	"r3xx": 0,
	"r4xx": 0,
	"r5xx": 0,
	"runk": 0,
}

def handle_result(url_info, record_info, error_info={}, http_info={}):
	#print("url_info", url_info)
	#print("record_info", record_info)
	#print("error_info", error_info)
	#print("http_info", http_info)

	update_igoff_in_job_data()

	response_code = 0
	if http_info.get("response_code"):
		response_code = http_info.get("response_code")
		response_code_str = str(http_info["response_code"])
		if len(response_code_str) == 3 and response_code_str[0] in "12345":
			job_data["r%sxx" % response_code_str[0]] += 1
		else:
			job_data["runk"] += 1

	if http_info.get("body"):
		job_data["bytes_downloaded"] += http_info["body"]["content_size"]

	stop = should_stop()

	response_message = http_info.get("response_message")
	if error_info:
		response_code = 0
		response_message = error_info["error"]

	if ws_factory.client:
		ws_factory.client.send_object({
			"type": "download",
			"job_data": job_data,
			"url": url_info["url"],
			"response_code": response_code,
			"response_message": response_message,
		})

	if stop:
		return wpull_hook.actions.STOP

	return wpull_hook.actions.NORMAL


def handle_response(url_info, record_info, http_info):
	return handle_result(url_info, record_info, http_info=http_info)


def handle_error(url_info, record_info, error_info):
	return handle_result(url_info, record_info, error_info=error_info)


# TODO: check only every 5 seconds max
def should_stop():
	return os.path.exists(os.path.join(working_dir, "stop"))


# TODO: check only every 5 seconds max
def update_igoff_in_job_data():
	igoff = os.path.exists(os.path.join(working_dir, "igoff"))
	job_data["suppress_ignore_reports"] = igoff
	return igoff


def maybe_log_ignore(url, pattern):
	if not update_igoff_in_job_data():
		print_to_real("IGNOR %s by %s" % (url, pattern))
		if ws_factory.client:
			ws_factory.client.send_object({
				"type": "ignore",
				"job_data": job_data,
				"url": url,
				"pattern": pattern
			})


# Regular expressions for server headers go here
ICY_FIELD_PATTERN = re.compile('icy-|ice-|x-audiocast-', re.IGNORECASE)
ICY_VALUE_PATTERN = re.compile('icecast', re.IGNORECASE)

def handle_pre_response(url_info, url_record, response_info):
	url = url_info['url']

	# Check if server version starts with ICY
	if response_info.get('version', '') == 'ICY':
		maybe_log_ignore(url, '[icy version]')
		return wpull_hook.actions.FINISH

	# Loop through all the server headers for matches
	for field, value in response_info.get('fields', []):
		if ICY_FIELD_PATTERN.match(field):
			maybe_log_ignore(url, '[icy field]')
			return wpull_hook.actions.FINISH

		if field == 'Server' and ICY_VALUE_PATTERN.match(value):
			maybe_log_ignore(url, '[icy server]')
			return wpull_hook.actions.FINISH

	# Nothing matched, allow download
	print_to_real(url + " ...")
	return wpull_hook.actions.NORMAL


def stdout_write_both(message):
	assert isinstance(message, bytes), message
	try:
		real_stdout_write(message)
		if ws_factory.client:
			ws_factory.client.send_object({
				"type": "stdout",
				"job_data": job_data,
				"message": message.decode("utf-8")
			})
	except Exception as e:
		real_stderr_write((str(e) + "\n").encode("utf-8"))


def stderr_write_both(message):
	assert isinstance(message, bytes), message
	try:
		real_stderr_write(message)
		if ws_factory.client:
			ws_factory.client.send_object({
				"type": "stderr",
				"job_data": job_data,
				"message": message.decode("utf-8")
			})
	except Exception as e:
		real_stderr_write((str(e) + "\n").encode("utf-8"))

sys.stdout.buffer.write = stdout_write_both
sys.stderr.buffer.write = stderr_write_both


def exit_status(code):
	print()
	print("Finished grab {} {} with exit code {}".format(
		job_data["ident"], job_data["url"], code))
	print("Output is in directory:\n{}".format(working_dir))
	return code


assert 2 in wpull_hook.callbacks.AVAILABLE_VERSIONS

wpull_hook.callbacks.version = 2
wpull_hook.callbacks.accept_url = accept_url
wpull_hook.callbacks.queued_url = queued_url
wpull_hook.callbacks.dequeued_url = dequeued_url
wpull_hook.callbacks.handle_response = handle_response
wpull_hook.callbacks.handle_error = handle_error
wpull_hook.callbacks.handle_pre_response = handle_pre_response
wpull_hook.callbacks.exit_status = exit_status
