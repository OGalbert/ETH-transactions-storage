# Indexer for Ethereum to get transaction list by ETH address
# https://github.com/Adamant-im/ETH-transactions-storage

# Contributions:
# 2021-2022 ADAMANT Foundation (devs@adamant.im), Francesco Bonanno (mibofra@parrotsec.org),
# Guénolé de Cadoudal (guenoledc@yahoo.fr), Drew Wells (drew.wells00@gmail.com)
# 2020-2021 ADAMANT Foundation (devs@adamant.im): Aleksei Lebedev
# 2017-2020 ADAMANT TECH LABS LP (pr@adamant.im): Artem Brunov, Aleksei Lebedev
# v2.1

from os import environ
from web3 import Web3
from web3.middleware import geth_poa_middleware
import psycopg2
import time
import sys
import logging

# from systemd.journal import JournalHandler

# Get env variables or set to default
dbname = environ.get("DB_NAME")
username = environ.get("DB_USER")
userpass = environ.get("DB_PASS")

startBlock = environ.get("START_BLOCK") or "1"
confirmationBlocks = environ.get("CONFIRMATIONS_BLOCK") or "0"
reorg_blocks = environ.get("CONFIRMATIONS_BLOCK") or 10
nodeUrl = environ.get("ETH_URL")
pollingPeriod = environ.get("PERIOD") or "20"
logFile = environ.get("LOG_FILE")

if dbname == None:
    print("Add postgre database in env var DB_NAME")
    exit(2)

if nodeUrl == None:
    print("Add eth url in env var ETH_URL")
    exit(2)

# Connect to Ethereum node
if nodeUrl.startswith("http"):
    web3 = Web3(Web3.HTTPProvider(nodeUrl))  # "http://publicnode:8545"
elif nodeUrl.startswith("ws"):
    web3 = Web3(Web3.WebsocketProvider(nodeUrl))  # "ws://publicnode:8546"
else:
    web3 = Web3(Web3.IPCProvider(nodeUrl))  # "/home/geth/.ethereum/geth.ipc"

web3.middleware_onion.inject(geth_poa_middleware, layer=0)

# Start logger
# logger = logging.getLogger("EthIndexerLog")
logger = logging.getLogger("eth-sync")
logger.setLevel(logging.INFO)

# File logger
if logFile == None:
    lfh = logging.StreamHandler()
else:
    lfh = logging.FileHandler(logFile)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
lfh.setFormatter(formatter)
logger.addHandler(lfh)

# Systemd logger, if we want to user journalctl logs
# Install systemd-python and
# decomment "#from systemd.journal import JournalHandler" up
# ljc = JournalHandler()
# formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
# ljc.setFormatter(formatter)
# logger.addHandler(ljc)

try:
    logger.info("Trying to connect to " + dbname + " database…")
    pg_connection_dict = {"dbname": dbname, "user": username, "password": userpass}
    conn = psycopg2.connect(**pg_connection_dict)
    conn.autocommit = True
    logger.info("Connected to the database")
except:
    logger.error("Unable to connect to database")
    exit(1)

# Delete last block as it may be not imported in full
cur = conn.cursor()
cur.execute(
    "DELETE FROM public.ethtxs WHERE block = (SELECT Max(block) from public.ethtxs)"
)
cur.close()
conn.close()

# Wait for the node to be in sync before indexing
while web3.eth.syncing != False:
    # Change with the time, in second, do you want to wait
    # before checking again, default is 5 minutes
    logger.info("Waiting Ethereum node to be in sync…")
    time.sleep(300)

logger.info("Ethereum node is synced.")

# Adds all transactions from Ethereum block
def insertTxsFromBlock(block):
    blockid = block["number"]
    time = block["timestamp"]
    for txNumber in range(0, len(block.transactions)):
        trans = block.transactions[txNumber]
        transReceipt = web3.eth.getTransactionReceipt(trans["hash"])
        # Save also transaction status, should be null if pre byzantium blocks
        status = bool(transReceipt["status"])
        txhash = trans["hash"].hex()
        value = trans["value"]
        inputinfo = trans["input"]

        contract_to = ""
        contract_value = ""
        # Check if transaction is a contract transfer
        if inputinfo.startswith("0xa9059cbb"):
            # contract_to = inputinfo[10:-64]
            # typical output if there is a contract involved:
            # 0000000000000000000000009f066b6ddc399dcbc7c596ad7d97b79247c85afb
            # We transform it by -1) taking the last 40 characters
            # -2) adding 0x to the front
            # Resulting hash should match the standard address (or contract) hash format
            #
            # contract_to = f'0x{contract_to[-40:]}'
            # contract_value = inputinfo[74:]
            contract_to = f"0x{inputinfo[-104 :-64]}"
            contract_value = inputinfo[-64:]
            if inputinfo[-128:-104] != "000000000000000000000000":
                logger.warning(
                    f"Address input part doesnt have 24 leading zeros: {inputinfo[-128 :-104]}. Txhash: {txhash}"
                )

        if value == 0 and not inputinfo.startswith("0xa9059cbb"):
            continue
        try:
            fr = trans["from"]
        except:
            logger.error(
                f"Cannot get 'from' item from transaction. txhash : ({txhash})"
            )
        try:
            to = trans["to"]
        except:
            logger.error(f"Cannot get 'to' item from transaction.  txhash :({txhash})")

        gasprice = trans["gasPrice"]
        gas = transReceipt["gasUsed"]

        cur.execute(
            "INSERT INTO public.ethtxs(time, txfrom, txto, value, gas, gasprice, block, txhash, contract_to, contract_value, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                time,
                fr,
                to,
                value,
                gas,
                gasprice,
                blockid,
                txhash,
                contract_to,
                contract_value,
                status,
            ),
        )

def delete_last_blocks_from_db(begin_block, end_block):
    try:
        cur.execute("DELETE from ethtxs CASCADE WHERE block > %s AND block <= %s", (begin_block, end_block))
        logger.info(f'Deleted blocks {begin_block+1} to {end_block}')
    except:
        logger.error(f'Failed to delete blocks {begin_block+1} to {end_block}')

latest_hash = ''
# Fetch all of new (not in index) Ethereum blocks and add transactions to index
while True:
    try:
        pg_connection_dict = {"dbname": dbname, "user": username, "password": userpass}
        conn = psycopg2.connect(**pg_connection_dict)
        conn.autocommit = True
    except:
        logger.error("Unable to connect to database")

    cur = conn.cursor()

    cur.execute("SELECT Max(block) from public.ethtxs")
    maxblockindb = cur.fetchone()[0]
    # On first start, we index transactions from a block number you indicate
    if maxblockindb is None:
        maxblockindb = int(startBlock)

    endblock = int(web3.eth.blockNumber) - int(confirmationBlocks)

    logger.info(
        "Current best block in index: "
        + str(maxblockindb)
        + "; in Ethereum chain: "
        + str(endblock)
    )



    for blockHeight in range(maxblockindb + 1, endblock+1):
        block = web3.eth.getBlock(blockHeight, True)
        if blockHeight == endblock:
            if latest_hash != block['parentHash'].hex():
                # then we have to delete the lastest x blocks
                logger.warning(f'Reorganisation!: Lastest hash is not the same as the newest ParentHash')
                delete_last_blocks_from_db(blockHeight-1-reorg_blocks, blockHeight-1)
                continue

        if len(block.transactions) > 0:
            insertTxsFromBlock(block)
            logger.info(
                "Block "
                + str(blockHeight)
                + " with "
                + str(len(block.transactions))
                + " transactions is processed"
            )
        else:
            logger.info("Block " + str(blockHeight) + " does not contain transactions")
        latest_hash = block['hash'].hex()
    cur.close()
    conn.close()
    time.sleep(int(pollingPeriod))
