'use strict'

const assert = require('node:assert')
const fs = require('node:fs')
const http = require('node:http')
const net = require('node:net')
const os = require('node:os')
const path = require('node:path')
const { spawn, spawnSync } = require('node:child_process')

const ROOT = path.resolve(__dirname, '..')
const EXAMPLES = path.join(ROOT, 'examples')
const STARTED = /Express started on port (\d+)/

// Examples that start without external services, with one request each.
const RUNNABLE = [
  ['hello-world', '/', 200],
  ['content-negotiation', '/', 200],
  ['cookie-sessions', '/', 200],
  ['cookies', '/', 200],
  ['downloads', '/', 200],
  ['ejs', '/', 200],
  ['error', '/', 500],
  ['error-pages', '/', 200],
  ['markdown', '/', 200],
  ['multi-router', '/', 200],
  ['mvc', '/', 302],
  ['params', '/', 200],
  ['resource', '/', 200],
  ['route-map', '/', 404],
  ['route-middleware', '/', 302],
  ['route-separation', '/', 200],
  ['view-constructor', '/', 200],
  ['view-locals', '/', 200],
  ['web-service', '/api/users?api-key=foo', 200],
  ['auth', '/', 302]
]

function freePort () {
  return new Promise(function (resolve, reject) {
    const server = net.createServer()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', function () {
      const port = server.address().port
      server.close(function () { resolve(port) })
    })
  })
}

function request (port, pathname, headers) {
  return new Promise(function (resolve, reject) {
    const req = http.get({ host: '127.0.0.1', port, path: pathname, headers: headers || {} }, function (res) {
      let body = ''
      res.setEncoding('utf8')
      res.on('data', function (chunk) { body += chunk })
      res.on('end', function () { resolve({ status: res.statusCode, body, headers: res.headers }) })
    })
    req.setTimeout(5000, function () { req.destroy(new Error('request timeout')) })
    req.on('error', reject)
  })
}

function launch (script, env) {
  const child = spawn(process.execPath, [script], {
    cwd: ROOT,
    env: Object.assign({}, process.env, { NODE_ENV: '' }, env)
  })
  const run = { child, stdout: '', stderr: '' }
  child.stdout.setEncoding('utf8')
  child.stderr.setEncoding('utf8')
  child.stdout.on('data', function (chunk) { run.stdout += chunk })
  child.stderr.on('data', function (chunk) { run.stderr += chunk })
  run.exited = new Promise(function (resolve) {
    child.on('exit', function (code) { resolve(code) })
  })
  run.started = new Promise(function (resolve, reject) {
    const timer = setTimeout(function () {
      reject(new Error('no start message from ' + script + '\nstdout: ' + run.stdout + '\nstderr: ' + run.stderr))
    }, 8000)
    child.stdout.on('data', function () {
      const match = STARTED.exec(run.stdout)
      if (match) {
        clearTimeout(timer)
        resolve(Number(match[1]))
      }
    })
    run.exited.then(function (code) {
      clearTimeout(timer)
      reject(new Error(script + ' exited with ' + code + '\nstdout: ' + run.stdout + '\nstderr: ' + run.stderr))
    })
  })
  run.started.catch(function () {})
  return run
}

function stop (run) {
  run.child.kill()
  return run.exited
}

function sleep (ms) {
  return new Promise(function (resolve) { setTimeout(resolve, ms) })
}

function required (code, env) {
  const result = spawnSync(process.execPath, ['-e', code], {
    cwd: ROOT,
    env: Object.assign({}, process.env, { NODE_ENV: '' }, env),
    encoding: 'utf8',
    timeout: 8000
  })
  return result
}

function exampleFiles (dir) {
  const found = []
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name === 'public') continue
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) found.push(...exampleFiles(full))
    else if (entry.name.endsWith('.js')) found.push(full)
  }
  return found
}

describe('HeldOut examples/start helper', function () {
  this.timeout(20000)

  describe('start(entry, app, port)', function () {
    let dir
    const start = path.join(EXAMPLES, 'start.js')

    before(function () {
      dir = fs.mkdtempSync(path.join(os.tmpdir(), 'express-start-'))
      fs.writeFileSync(path.join(dir, 'app.js'), [
        "const express = require(" + JSON.stringify(ROOT) + ")",
        "const { start } = require(" + JSON.stringify(start) + ")",
        "const app = module.exports = express()",
        "app.get('/', (req, res) => res.send('ok'))",
        "const port = process.env.TEST_DEFAULT_PORT === undefined ? undefined : Number(process.env.TEST_DEFAULT_PORT)",
        "const server = start(module, app, port)",
        "if (require.main === module) console.log('returned ' + (server && typeof server.close))"
      ].join('\n'))
      fs.writeFileSync(path.join(dir, 'entry.js'), [
        "const app = require('./app')",
        "console.log('listening ' + Boolean(app.listen && false))"
      ].join('\n'))
    })

    after(function () {
      fs.rmSync(dir, { recursive: true, force: true })
    })

    it('exports isMain, quiet, and start', function () {
      const helper = require(start)
      assert.strictEqual(typeof helper.isMain, 'function')
      assert.strictEqual(typeof helper.quiet, 'function')
      assert.strictEqual(typeof helper.start, 'function')
      assert.strictEqual(helper.isMain({}), false)
      assert.strictEqual(helper.isMain(require.main), true)
    })

    it('quiet follows NODE_ENV=test', function () {
      const result = required(
        'process.stdout.write(String(require(' + JSON.stringify(start) + ').quiet()))',
        { NODE_ENV: 'test' }
      )
      assert.strictEqual(result.stdout, 'true')
      const loud = required('process.stdout.write(String(require(' + JSON.stringify(start) + ').quiet()))', { NODE_ENV: 'production' })
      assert.strictEqual(loud.stdout, 'false')
    })

    it('listens on PORT and reports the bound port when run directly', async function () {
      const port = await freePort()
      const run = launch(path.join(dir, 'app.js'), { PORT: String(port) })
      try {
        assert.strictEqual(await run.started, port)
        assert.match(run.stdout, /returned function/)
        assert.strictEqual((await request(port, '/')).body, 'ok')
      } finally {
        await stop(run)
      }
    })

    it('reports the real port when PORT is 0', async function () {
      const run = launch(path.join(dir, 'app.js'), { PORT: '0' })
      try {
        const port = await run.started
        assert.ok(port > 0, 'port ' + port)
        assert.strictEqual((await request(port, '/')).body, 'ok')
      } finally {
        await stop(run)
      }
    })

    it('uses the default port argument when PORT is unset or invalid', async function () {
      for (const portEnv of [undefined, 'abc', '']) {
        const port = await freePort()
        const env = { TEST_DEFAULT_PORT: String(port) }
        if (portEnv !== undefined) env.PORT = portEnv
        else env.PORT = ''
        const run = launch(path.join(dir, 'app.js'), env)
        try {
          assert.strictEqual(await run.started, port)
        } finally {
          await stop(run)
        }
      }
    })

    it('prefers PORT over the default port argument', async function () {
      const [port, other] = [await freePort(), await freePort()]
      const run = launch(path.join(dir, 'app.js'), { PORT: String(port), TEST_DEFAULT_PORT: String(other) })
      try {
        assert.strictEqual(await run.started, port)
      } finally {
        await stop(run)
      }
    })

    it('does not listen, log, or return a server when required', function () {
      const result = required('require(' + JSON.stringify(path.join(dir, 'entry.js')) + ')', { PORT: '0' })
      assert.strictEqual(result.status, 0, result.stderr + result.error)
      assert.strictEqual(result.stdout.trim(), 'listening false')
      const direct = required(
        "const { start } = require(" + JSON.stringify(start) + "); " +
        "console.log(String(start(module, require(" + JSON.stringify(ROOT) + ")())))",
        { PORT: '0' }
      )
      assert.strictEqual(direct.status, 0, direct.stderr)
      assert.strictEqual(direct.stdout.trim(), 'undefined')
    })
  })

  describe('migrated examples', function () {
    for (const [name, pathname, status] of RUNNABLE) {
      it(name + ' starts through the helper when run directly', async function () {
        const port = await freePort()
        const run = launch(path.join('examples', name, 'index.js'), { PORT: String(port) })
        try {
          assert.strictEqual(await run.started, port)
          const res = await request(port, pathname)
          assert.strictEqual(res.status, status)
        } finally {
          await stop(run)
        }
      })

      if (name !== 'route-map') it(name + ' stays silent and does not listen when required', function () {
        const result = required('require(' + JSON.stringify(path.join(EXAMPLES, name, 'index.js')) + ')', { PORT: '0' })
        assert.strictEqual(result.status, 0, result.stderr + result.error)
        assert.strictEqual(result.stdout, '')
        assert.strictEqual(result.stderr, '')
      })
    }

    it('vhost serves the main app and starts through the helper', async function () {
      const port = await freePort()
      const run = launch(path.join('examples', 'vhost', 'index.js'), { PORT: String(port) })
      try {
        assert.strictEqual(await run.started, port)
        const res = await request(port, '/', { Host: 'example.com' })
        assert.strictEqual(res.status, 200)
        assert.strictEqual(res.body, 'Hello from main app!')
        await sleep(100)
        assert.match(run.stdout, /GET \/ \S*200/, 'dev logger is active when run directly')
        const sub = await request(port, '/', { Host: 'sub.example.com' })
        assert.strictEqual(sub.status, 302)
        await sleep(100)
        assert.match(run.stdout, /hostname: 'sub\.example\.com'/, 'redirect app logs req.vhost when run directly')
      } finally {
        await stop(run)
      }
    })

    it('vhost is silent when required and still redirects', function () {
      const result = required(
        "const request = require('supertest'); " +
        "request(require(" + JSON.stringify(path.join(EXAMPLES, 'vhost')) + ")).get('/').set('Host', 'sub.example.com').end((err, res) => { " +
        "console.error('status ' + res.status); process.exit(0) })",
        {}
      )
      assert.strictEqual(result.stdout, '')
      assert.strictEqual(result.stderr.trim(), 'status 302')
    })

    it('static-files starts through the helper and keeps its hints', async function () {
      const port = await freePort()
      const run = launch(path.join('examples', 'static-files', 'index.js'), { PORT: String(port) })
      try {
        assert.strictEqual(await run.started, port)
        const res = await request(port, '/hello.txt')
        assert.strictEqual(res.status, 200)
        assert.match(run.stdout, /try:\n\s+GET \/hello\.txt\n\s+GET \/js\/app\.js\n\s+GET \/css\/style\.css/)
      } finally {
        await stop(run)
      }
    })

    it('static-files stays silent and does not listen when required', function () {
      const result = required('require(' + JSON.stringify(path.join(EXAMPLES, 'static-files', 'index.js')) + ')', { PORT: '0' })
      assert.strictEqual(result.status, 0, result.stderr + result.error)
      assert.strictEqual(result.stdout, '')
    })

    it('mvc logs its routes and requests only when run directly', async function () {
      const port = await freePort()
      const run = launch(path.join('examples', 'mvc', 'index.js'), { PORT: String(port) })
      try {
        assert.strictEqual(await run.started, port)
        assert.match(run.stdout, /GET \/users/, 'boot logs routes when run directly')
        await request(port, '/users')
        await sleep(100)
        assert.match(run.stdout, /GET \/users \S*200/, 'dev logger is active when run directly')
        assert.strictEqual((await request(port, '/nope')).status, 404)
      } finally {
        await stop(run)
      }
      const quiet = required(
        "const request = require('supertest'); " +
        "request(require(" + JSON.stringify(path.join(EXAMPLES, 'mvc')) + ")).get('/users').end((err, res) => { " +
        "console.error('status ' + res.status); process.exit(0) })",
        {}
      )
      assert.strictEqual(quiet.stdout, '')
      assert.strictEqual(quiet.stderr.trim(), 'status 200')
    })

    it('auth logs authentication attempts only when run directly', async function () {
      const port = await freePort()
      const run = launch(path.join('examples', 'auth', 'index.js'), { PORT: String(port) })
      try {
        assert.strictEqual(await run.started, port)
        await new Promise(function (resolve, reject) {
          const body = 'username=tj&password=foobar'
          const req = http.request({
            host: '127.0.0.1', port, path: '/login', method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'Content-Length': Buffer.byteLength(body) }
          }, function (res) { res.resume(); res.on('end', resolve) })
          req.on('error', reject)
          req.end(body)
        })
        assert.match(run.stdout, /authenticating tj:foobar/)
      } finally {
        await stop(run)
      }
      const quiet = required(
        "const request = require('supertest'); " +
        "request(require(" + JSON.stringify(path.join(EXAMPLES, 'auth')) + ")).post('/login').type('form').send({ username: 'tj', password: 'foobar' }).end((err, res) => { " +
        "console.error('status ' + res.status); process.exit(0) })",
        {}
      )
      assert.strictEqual(quiet.stdout, '')
    })

    it('error-pages logs requests only when run directly', async function () {
      const port = await freePort()
      const run = launch(path.join('examples', 'error-pages', 'index.js'), { PORT: String(port) })
      try {
        assert.strictEqual(await run.started, port)
        assert.strictEqual((await request(port, '/500')).status, 500)
        await sleep(100)
        assert.match(run.stdout, /GET \/500 \S*500/, 'dev logger is active when run directly')
      } finally {
        await stop(run)
      }
    })

    it('quiet() silences cookies request logging and route-map listing under NODE_ENV=test', async function () {
      for (const [name, probe] of [['cookies', 'GET /'], ['route-map', 'get /users']]) {
        const port = await freePort()
        const loud = launch(path.join('examples', name, 'index.js'), { PORT: String(port) })
        let loudOut
        try {
          await loud.started
          await request(port, '/')
          await sleep(150)
          loudOut = loud.stdout.replace(STARTED, '')
        } finally {
          await stop(loud)
        }
        assert.ok(loudOut.includes(probe), name + ' prints without NODE_ENV=test: ' + JSON.stringify(loudOut))
        const quietPort = await freePort()
        const quiet = launch(path.join('examples', name, 'index.js'), { PORT: String(quietPort), NODE_ENV: 'test' })
        try {
          await quiet.started
          await request(quietPort, '/')
          await sleep(150)
          assert.strictEqual(quiet.stdout.replace(STARTED, '').trim(), '', name + ' is quiet under NODE_ENV=test')
        } finally {
          await stop(quiet)
        }
      }
    })
  })

  describe('source migration', function () {
    it('no example reads module.parent', function () {
      const offenders = exampleFiles(EXAMPLES).filter(function (file) {
        return /module\.parent/.test(fs.readFileSync(file, 'utf8'))
      })
      assert.deepStrictEqual(offenders.map(function (f) { return path.relative(ROOT, f) }), [])
    })

    it('only examples/start.js calls listen', function () {
      const offenders = exampleFiles(EXAMPLES).filter(function (file) {
        return path.basename(file) !== 'start.js' && /\.listen\(/.test(fs.readFileSync(file, 'utf8'))
      })
      assert.deepStrictEqual(offenders.map(function (f) { return path.relative(ROOT, f) }), [])
    })

    it('only examples/start.js reads process.env.NODE_ENV in code', function () {
      const offenders = exampleFiles(EXAMPLES).filter(function (file) {
        if (path.basename(file) === 'start.js') return false
        return fs.readFileSync(file, 'utf8').split('\n').some(function (line) {
          return /process\.env\.NODE_ENV/.test(line) && !/^\s*(\/\/|\*)/.test(line)
        })
      })
      assert.deepStrictEqual(offenders.map(function (f) { return path.relative(ROOT, f) }), [])
    })
  })
})
