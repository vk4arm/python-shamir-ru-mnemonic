import secrets
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Set, Tuple

import attr
import click
from click import style

from .constants import GROUP_PREFIX_LENGTH_WORDS
from .shamir import combine_mnemonics, generate_mnemonics
from .share import Share
from .utils import MnemonicError


@click.group()
def cli() -> None:
    pass


@cli.command()
@click.argument("scheme")
@click.option(
    "-g",
    "--group",
    "groups",
    type=(int, int),
    metavar="T N",
    multiple=True,
    help="Задайте K из N кастомную схему",
)
@click.option(
    "-t",
    "--threshold",
    type=int,
    help="Для кастомной схемы нужно задать количество групп",
)
@click.option("-E", "--exponent", type=int, default=0, help="Iteration exponent.")
@click.option(
    "-s", "--strength", type=int, default=128, help="Мощность секрета в битах"
)
@click.option(
    "-S", "--master-secret", help="16-ричный мастер-ключ", metavar="HEX"
)
@click.option("-p", "--passphrase", help="Пароль для восстановления")
def create(
    scheme: str,
    groups: Sequence[Tuple[int, int]],
    threshold: int,
    exponent: int,
    master_secret: str,
    passphrase: str,
    strength: int,
) -> None:
    """Создание мнемонического разделенного методом Шамира секрета с расово-верным уклоном

    Схема может быть одной из:

    \b
    single: Создается 1 сид для восстановления.
    2of3: Создается 3 секрета. Для восстановления нужно использовать любые 2 из 3.
          (Максимальное количество секретов - 16. Например: 13of14 - будет работать, а 14of88 - нет)
    master: Создается 1 мастер-секрет, из которого можно восстановить основной ключ,
            плюс группа 3-из-5: 5 секретов, с 3 нужными для восстановления.
            Сделайте себе тату с мастер-секретом, а 5 - пусть сделают себе тату друзья.
    custom: Задайте кастомную конфигурацию через параметры -t и -g
    """
    if passphrase and not master_secret:
        raise click.ClickException(
            "Используйте кодовую фразу только вместе с явным главным секретом"
        )

    if (groups or threshold is not None) and scheme != "custom":
        raise click.BadArgumentUsage(f"Для использования -g/-t, вам нужно выбрать 'custom' схему.")

    if scheme == "single":
        threshold = 1
        groups = [(1, 1)]
    elif scheme == "master":
        threshold = 1
        groups = [(1, 1), (3, 5)]
    elif "of" in scheme:
        try:
            m, n = map(int, scheme.split("of", maxsplit=1))
            threshold = 1
            groups = [(m, n)]
        except Exception as e:
            raise click.BadArgumentUsage(f"Invalid scheme: {scheme}") from e
    elif scheme == "custom":
        if threshold is None:
            raise click.BadArgumentUsage(
                "Используйте '-t' для определения к-во групп для восстановления"
            )
        if not groups:
            raise click.BadArgumentUsage(
                "Используйте '-g T N' чтобы добавить T-of-N группу в коллекцию"
            )
    else:
        raise click.ClickException(f"Unknown scheme: {scheme}")

    if any(m == 1 and n > 1 for m, n in groups):
        click.echo("1-of-X groups are not allowed.")
        click.echo("Instead, set up a 1-of-1 group and give everyone the same share.")
        sys.exit(1)

    if master_secret is not None:
        try:
            secret_bytes = bytes.fromhex(master_secret)
        except Exception as e:
            raise click.BadOptionUsage(
                "master_secret", f"Secret bytes must be hex encoded"
            ) from e
    else:
        secret_bytes = secrets.token_bytes(strength // 8)

    secret_hex = style(secret_bytes.hex(), bold=True)
    click.echo(f"Using master secret: {secret_hex}")

    if passphrase:
        try:
            passphrase_bytes = passphrase.encode("ascii")
        except UnicodeDecodeError:
            raise click.ClickException("Passphrase must be ASCII only")
    else:
        passphrase_bytes = b""

    mnemonics = generate_mnemonics(
        threshold, groups, secret_bytes, passphrase_bytes, exponent
    )

    for i, (group, (m, n)) in enumerate(zip(mnemonics, groups)):
        group_str = (
            style("Group ", fg="green")
            + style(str(i + 1), bold=True)
            + style(f" of {len(mnemonics)}", fg="green")
        )
        share_str = style(f"{m} of {n}", fg="blue", bold=True) + style(
            " shares required:", fg="blue"
        )
        click.echo(f"{group_str} - {share_str}")
        for g in group:
            click.echo(g)


FINISHED = style("\u2713", fg="green", bold=True)
EMPTY = style("\u2717", fg="red", bold=True)
INPROGRESS = style("\u26ec", fg="yellow", bold=True)


def error(s: str) -> None:
    click.echo(style("ERROR: ", fg="red") + s)


@cli.command()
@click.option(
    "-p", "--passphrase-prompt", is_flag=True, help="Use passphrase after recovering"
)
def recover(passphrase_prompt: bool) -> None:
    last_share: Optional[Share] = None
    all_mnemonics: List[str] = []
    groups: Dict[int, Set[Share]] = defaultdict(set)  # group idx : shares

    def make_group_prefix(idx: int) -> str:
        assert last_share is not None
        fake_share = attr.evolve(last_share, group_index=idx)
        return " ".join(fake_share.words()[:GROUP_PREFIX_LENGTH_WORDS])

    def print_group_status(idx: int) -> None:
        group = groups[idx]
        group_prefix = style(make_group_prefix(idx), bold=True)
        bi = style(str(len(group)), bold=True)
        if not group:
            click.echo(f"{EMPTY} {bi} shares from group {group_prefix}")
        else:
            elem = next(iter(group))
            prefix = FINISHED if len(group) >= elem.threshold else INPROGRESS
            bt = style(str(elem.threshold), bold=True)
            click.echo(f"{prefix} {bi} of {bt} shares needed from group {group_prefix}")

    def group_is_complete(idx: int) -> bool:
        group = groups[idx]
        if not group:
            return False
        return len(group) >= next(iter(group)).threshold

    def print_status() -> None:
        assert last_share is not None
        n_completed = len([idx for idx in groups if group_is_complete(idx)])
        bn = style(str(n_completed), bold=True)
        bt = style(str(last_share.group_threshold), bold=True)
        click.echo()
        if last_share.group_count > 1:
            click.echo(f"Completed {bn} of {bt} groups needed:")
        for i in range(last_share.group_count):
            print_group_status(i)

    def set_is_complete() -> bool:
        assert last_share is not None
        n_completed = len([idx for idx in groups if group_is_complete(idx)])
        return n_completed >= last_share.group_threshold

    while last_share is None or not set_is_complete():
        try:
            mnemonic_str = click.prompt("Enter a recovery share")
            share = Share.from_mnemonic(mnemonic_str)

            if last_share is None:
                last_share = share

            if last_share.common_parameters() != share.common_parameters():
                error("This mnemonic is not part of the current set. Please try again.")

            else:
                last_share = share
                groups[share.group_index].add(share)
                all_mnemonics.append(mnemonic_str)

            print_status()

        except click.Abort:
            return
        except Exception as e:
            error(str(e))

    passphrase_bytes = b""
    if passphrase_prompt:
        while True:
            passphrase = click.prompt(
                "Enter passphrase", hide_input=True, confirmation_prompt=True
            )
            try:
                passphrase_bytes = passphrase.encode("ascii")
                break
            except UnicodeDecodeError:
                click.echo("Passphrase must be ASCII. Please try again.")

    try:
        master_secret = combine_mnemonics(all_mnemonics, passphrase_bytes)
    except MnemonicError as e:
        error(str(e))
        click.echo("Recovery failed")
        sys.exit(1)
    click.secho("SUCCESS!", fg="green", bold=True)
    click.echo(f"Your master secret is: {master_secret.hex()}")


if __name__ == "__main__":
    cli()
