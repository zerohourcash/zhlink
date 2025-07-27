import uvicorn

dir_with_ssl = ''

if __name__ == "__main__":
    uvicorn.run(
        "zhlink_lite_2:app",
        host="0.0.0.0",
        port=443,
        reload=True,
        ssl_keyfile=f'{dir_with_ssl}/privkey.pem',
        ssl_certfile=f'{dir_with_ssl}/fullchain.pem'
    )